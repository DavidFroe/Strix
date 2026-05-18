"""
owltrail_adapter.py — Adapter zwischen DeepSeek-TUI und owltrail.

- Liest owltrail.conf
- Mappt OpenAI-/DeepSeek-Modellnamen (deepseek-chat, deepseek-v4-pro, fast, ...)
  auf die numerischen QuiteQue-Modell-IDs aus owltrail.conf["models"].
- Startet OwlTrailProxy mit Monkey-Patch im Handler: jeder Request bekommt
  sein "model"-Feld dynamisch umgeschrieben. owltrail.py wird NICHT angefasst.

Importierbar (für Tests) UND direkt ausführbar:
    python3 owltrail_adapter.py --port 8081
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request

import owltrail
from owltrail import OwlTrailProxy, load_conf

_logger = logging.getLogger("owltrail.adapter")


# ───────────────────────────────────────────────────────────────────────────
# Modell-Mapping (Name -> numerische ID)
# ───────────────────────────────────────────────────────────────────────────

# Dynamisches Mapping: id aus /v1/models → numerical_id
# Wird nach dem Adapter-Start durch _build_dynamic_model_map() befüllt.
_DYNAMIC_MODEL_MAP: dict[str, str] = {}


def _build_dynamic_model_map(port: int) -> None:
    """Holt /v1/models vom eigenen Adapter-Port und befüllt _DYNAMIC_MODEL_MAP.

    Mappt jede 'id' (z.B. 'alibaba-qwen3-max') direkt auf ihre 'numerical_id'
    (z.B. '31'), damit get_model_id() alle Modelle korrekt routet — auch solche
    die nicht in _NAME_ALIASES stehen.
    """
    global _DYNAMIC_MODEL_MAP
    try:
        url = f"http://127.0.0.1:{port}/v1/models"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
        mapping = {}
        for m in data.get("data", []):
            mid = m.get("id", "").strip()
            nid = str(m.get("numerical_id", "")).strip()
            if mid and nid and nid.isdigit():
                mapping[mid.lower()] = nid
        _DYNAMIC_MODEL_MAP = mapping
        _logger.info("Dynamisches Modell-Mapping: %d Einträge geladen.", len(mapping))
    except Exception as exc:
        _logger.warning("Dynamisches Modell-Mapping fehlgeschlagen: %s", exc)


# Statische Aliase auf die logischen Schlüssel aus owltrail.conf["models"]
_NAME_ALIASES = {
    # DeepSeek Pro v4 → models["deepseek-v4-pro"] = "231"
    "deepseek-chat":       "deepseek-v4-pro",
    "deepseek-reasoner":   "deepseek-v4-pro",
    "deepseek-v4-pro":     "deepseek-v4-pro",
    "deepseek-v4pro":      "deepseek-v4-pro",
    "deepseek-pro":        "deepseek-v4-pro",
    "pro":                 "deepseek-v4-pro",
    "default":             "deepseek-v4-pro",

    # DeepSeek Flash v4 → models["deepseek-v4-flash"] = "230"
    "deepseek-v4-flash":   "deepseek-v4-flash",
    "deepseek-v4flash":    "deepseek-v4-flash",
    "deepseek-flash":      "deepseek-v4-flash",
    "deepseek-fast":       "deepseek-v4-flash",
    "flash":               "deepseek-v4-flash",
    "fast":                "deepseek-v4-flash",
    "quick":               "deepseek-v4-flash",

    # Web (WebSearch)
    "deepseek-web":        "web",
    "web":                 "web",
    "search":              "web",
}


def _conf_path() -> str:
    """Pfad zu owltrail.conf — neben diesem Modul oder in owltrail.py-Dir."""
    here = os.path.dirname(os.path.realpath(__file__))
    local = os.path.join(here, "owltrail.conf")
    if os.path.exists(local):
        return local
    return os.path.join(os.path.dirname(os.path.realpath(owltrail.__file__)), "owltrail.conf")


def get_owltrail_url(port: int | None = None) -> str:
    """OpenAI-kompatible Basis-URL des lokalen owltrail-Proxys."""
    cfg = load_conf(_conf_path())
    p = port or int(cfg.get("listen_port", 8081))
    return f"http://127.0.0.1:{p}/v1"


def get_model_id(model_name: str | None) -> str:
    """Mappt einen freien Modellnamen auf eine numerische QuiteQue-ID.

    Reihenfolge:
      1. Leer/None → Default aus owltrail.conf["model_id"]
      2. Bereits numerisch (z.B. "215") → unverändert
      3. Bekannter Alias → owltrail.conf["models"][alias]
      4. Direkter Treffer in owltrail.conf["models"] (Key=Wert)
      5. Fallback: Default
    """
    cfg = load_conf(_conf_path())
    default_id = str(cfg.get("model_id", "215"))
    models = cfg.get("models") or {}

    if not model_name:
        return default_id

    name = str(model_name).strip()
    if not name:
        return default_id

    # 2) Schon numerisch?
    if name.isdigit():
        return name

    lower = name.lower()

    # 3) Alias?
    alias_key = _NAME_ALIASES.get(lower)
    if alias_key and alias_key in models:
        return str(models[alias_key])

    # 4) Direkter Treffer in models-Dict (owltrail.conf)?
    if lower in models:
        return str(models[lower])

    # 5) Dynamisches Mapping aus /v1/models (z.B. "alibaba-qwen3-max" → "31")
    if lower in _DYNAMIC_MODEL_MAP:
        return _DYNAMIC_MODEL_MAP[lower]

    # 6) Fallback
    return default_id


# ───────────────────────────────────────────────────────────────────────────
# Hilfsfunktion: SSE-Stream von Backend einsammeln → JSON Chat-Completion
# ───────────────────────────────────────────────────────────────────────────

def _collect_sse_to_json(handler, path, body):
    """Schickt Request mit stream=true ans Backend, sammelt SSE, gibt JSON zurück.

    Verwendet wenn der Client stream=false/null sendet: propeller erwartet dann
    eine gewöhnliche JSON-Antwort, aber das Backend liefert immer SSE.
    """
    # stream=true erzwingen damit das Backend überhaupt streamt
    modified_body = body
    if body:
        try:
            data = json.loads(body)
            data["stream"] = True
            modified_body = json.dumps(data).encode()
        except (ValueError, TypeError):
            pass

    req, _ = handler._build_request(path, modified_body)
    cfg = handler.__class__.cfg
    timeout = int(cfg.get("timeout", 1800))

    raw_chunks: list[bytes] = []
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            raw_chunks.append(chunk)
        resp.close()
    except urllib.error.HTTPError as exc:
        err_body = exc.read()
        _logger.error("HTTP %d vom Backend (non-stream collect): %s", exc.code, path)
        try:
            handler.send_response(exc.code)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(err_body)
        except OSError:
            pass
        return
    except Exception as exc:
        _logger.error("Backend-Fehler beim Einsammeln [%s]: %s", path, exc)
        msg = json.dumps({"error": {"message": str(exc), "type": "proxy_error"}}).encode()
        try:
            handler.send_response(502)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(msg)
        except OSError:
            pass
        return

    # SSE-Zeilen parsen
    raw_text = b"".join(raw_chunks).decode("utf-8", errors="replace")
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    last_chunk: dict | None = None
    finish_reason = "stop"

    for line in raw_text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
            if obj.get("id") == "hb":   # heartbeat überspringen
                continue
            last_chunk = obj
            choices = obj.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                c = delta.get("content")
                if c:
                    content_parts.append(c)
                r = delta.get("reasoning_content")
                if r:
                    reasoning_parts.append(r)
                fr = choices[0].get("finish_reason")
                if fr:
                    finish_reason = fr
        except (ValueError, TypeError):
            pass

    response_obj = {
        "id": (last_chunk or {}).get("id", "chatcmpl-collected"),
        "object": "chat.completion",
        "created": (last_chunk or {}).get("created", 0),
        "model": (last_chunk or {}).get("model", "unknown"),
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "".join(content_parts),
                "reasoning_content": "".join(reasoning_parts) if reasoning_parts else None,
            },
            "finish_reason": finish_reason,
        }],
        "usage": (last_chunk or {}).get("usage") or {},
    }
    response_bytes = json.dumps(response_obj).encode("utf-8")

    _logger.info("Non-stream collect fertig: %d Zeichen, finish=%s", len("".join(content_parts)), finish_reason)
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(response_bytes)))
        handler.end_headers()
        handler.wfile.write(response_bytes)
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


# ───────────────────────────────────────────────────────────────────────────
# Monkey-Patch: non-streaming Requests → JSON-Antwort statt SSE
# ───────────────────────────────────────────────────────────────────────────

_stream_patch_installed = False


def _override_models_context_window(handler, path) -> None:
    """Holt /v1/models vom Backend, überschreibt context_window-Werte aus owltrail.conf
    und schickt die gepatchte Antwort zurück.

    Nötig wenn das Modell mehr Kontext hat als der Broker meldet
    (z.B. Ollama long-context Modelfiles).
    """
    cfg = handler.__class__.cfg
    overrides: dict[str, int] = {}
    for k, v in (cfg.get("context_overrides") or {}).items():
        try:
            overrides[str(k)] = int(v)
        except (ValueError, TypeError):
            pass

    req, _ = handler._build_request(path, None)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        _logger.error("Models-Fetch fehlgeschlagen: %s", exc)
        msg = json.dumps({"error": str(exc)}).encode()
        handler.send_response(502)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(msg)
        return

    patched = 0
    for model in data.get("data") or []:
        nid = str(model.get("numerical_id", ""))
        if nid in overrides:
            model["context_window"] = overrides[nid]
            patched += 1
    if patched:
        _logger.info("context_window überschrieben für %d Modelle", patched)

    response_bytes = json.dumps(data).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(response_bytes)))
    handler.end_headers()
    handler.wfile.write(response_bytes)
    handler.wfile.flush()


def install_stream_handling_patch() -> None:
    """Patcht owltrail._Handler._proxy so, dass Requests ohne stream=true
    eine gewöhnliche JSON-Antwort bekommen statt text/event-stream.

    propeller sendet für manche Kommandos (exec, review …) stream=null/false
    und erwartet JSON. Das Backend liefert immer SSE. Dieser Patch überbrückt
    die Lücke, ohne owltrail.py zu verändern.
    """
    global _stream_patch_installed
    if _stream_patch_installed:
        return

    # The handler class still inherits BaseHTTPRequestHandler's default of
    # "HTTP/1.0", so every response goes out as `HTTP/1.0 200 OK` while the
    # body uses `Transfer-Encoding: chunked` — which is invalid per RFC 2068.
    # curl is tolerant, but reqwest classifies the response as a transport
    # error ("error sending request") and surfaces it as a "Network error"
    # banner in the TUI. Upgrade the status line to HTTP/1.1 so the chunked
    # body becomes RFC-conformant.
    owltrail._Handler.protocol_version = "HTTP/1.1"

    original_proxy = owltrail._Handler._proxy

    def _patched_proxy(self, path, body=None):
        # /chat/completions is a long, possibly-streaming exchange. After the
        # response is fully written we want the *server* to close the TCP
        # socket — reqwest's connection pool otherwise re-uses a stale socket
        # for the next request, which surfaces in the TUI as "Network error"
        # until the user restarts the binary. The patched handlers below all
        # set self.close_connection = True before returning.
        is_models = (self.command == "GET"
                     and path.rstrip("/").endswith("/v1/models"))
        if is_models:
            _override_models_context_window(self, path)
            self.close_connection = True
            return

        is_chat = (self.command == "POST"
                   and path.rstrip("/").endswith("/chat/completions"))
        if is_chat:
            wants_stream = False
            if body:
                try:
                    wants_stream = bool(json.loads(body).get("stream"))
                except (ValueError, TypeError):
                    pass
            if not wants_stream:
                _logger.debug("Non-stream request → collect-JSON path: %s", path)
                _collect_sse_to_json(self, path, body)
                self.close_connection = True
                return
        original_proxy(self, path, body)
        if is_chat:
            self.close_connection = True

    owltrail._Handler._proxy = _patched_proxy
    _stream_patch_installed = True


# ───────────────────────────────────────────────────────────────────────────
# Monkey-Patch: dynamisches Modell-Mapping im owltrail-Handler
# ───────────────────────────────────────────────────────────────────────────

_patched = False


def install_model_mapping_patch() -> None:
    """Patcht owltrail._Handler._build_request so, dass das model-Feld
    jedes Requests durch get_model_id() ersetzt wird.

    owltrail.py selbst wird nicht verändert — der Patch greift zur Laufzeit.
    """
    global _patched
    if _patched:
        return

    original = owltrail._Handler._build_request

    def _patched_build_request(self, path, body):
        # Nur Chat-/Completion-Endpunkte umschreiben
        if body and (path.endswith("/chat/completions")
                     or path.endswith("/completions")
                     or "/chat/completions" in path):
            try:
                data = json.loads(body)
                if isinstance(data, dict) and "model" in data:
                    mapped = get_model_id(data.get("model"))
                    if mapped:
                        data["model"] = mapped
                        body = json.dumps(data).encode()
            except (ValueError, TypeError):
                # Body kein JSON → unverändert weiterleiten
                pass
        return original(self, path, body)

    owltrail._Handler._build_request = _patched_build_request
    _patched = True


# ───────────────────────────────────────────────────────────────────────────
# Standalone-CLI: startet OwlTrailProxy mit installiertem Patch
# ───────────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="OwlTrail-Adapter (mit Modell-Mapping) für DeepSeek-TUI",
    )
    parser.add_argument("--port", type=int, default=None,
                        help="Listen-Port (Default: aus owltrail.conf)")
    parser.add_argument("--conf", default=None,
                        help="Pfad zur owltrail.conf (Default: neben diesem Skript)")
    parser.add_argument("--log", type=str, default=None,
                        help="Pfad für Log-Datei (Default: owltrail.log neben dem Skript)")
    parser.add_argument("--verbose", action="store_true",
                        help="DEBUG-Logging")
    parser.add_argument("--no-mapping", action="store_true",
                        help="Modell-Mapping deaktivieren (Roh-Proxy)")
    args = parser.parse_args(argv)

    conf_path = args.conf or _conf_path()
    cfg = load_conf(conf_path)
    port = args.port or int(cfg.get("listen_port", 8081))

    log_path = args.log or os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "owltrail.log"
    )
    owltrail.setup_logging(log_path, verbose=args.verbose)

    if not args.no_mapping:
        install_model_mapping_patch()

    install_stream_handling_patch()

    proxy = OwlTrailProxy(conf_file=conf_path)
    proxy.start(port=port)
    sys.stdout.write(f"[owltrail-adapter] listening on 127.0.0.1:{port}\n")
    sys.stdout.write(f"[owltrail-adapter] forwarding to "
                     f"{cfg.get('server_ip')}:{cfg.get('quiteque_port')} "
                     f"(user={cfg.get('username')})\n")
    sys.stdout.write(f"[owltrail-adapter] model mapping "
                     f"{'OFF' if args.no_mapping else 'ON'}\n")
    sys.stdout.flush()

    # Dynamisches Modell-Mapping aufbauen (nach Serverstart, kurze Wartezeit)
    if not args.no_mapping:
        time.sleep(0.5)
        _build_dynamic_model_map(port)

    stop = {"flag": False, "count": 0}

    def _shutdown(_sig, _frm):
        stop["flag"] = True
        stop["count"] += 1
        # Zweimaliges Signal → harter Exit, falls die saubere Abschaltung hängt.
        if stop["count"] >= 2:
            os._exit(130)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    # SIGHUP (z.B. Terminal schließen) ebenfalls sauber abfangen — nicht ignorieren.
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _shutdown)

    try:
        while not stop["flag"]:
            time.sleep(0.5)
    finally:
        # Server-Socket sofort freigeben, sonst bleibt der Port in TIME_WAIT
        # und der nächste start.sh-Aufruf erbt einen halb-toten Listener.
        srv = getattr(proxy, "_server", None)
        try:
            proxy.stop()
        except Exception:
            pass
        if srv is not None:
            try:
                srv.server_close()
            except Exception:
                pass
        sys.stdout.write("[owltrail-adapter] stopped\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
