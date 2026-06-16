"""
owltrail.py — Minimaler OpenAI-kompatibler Proxy für popel.

Leitet Anfragen von opencode an QuiteQue weiter.
Auth: X-OwlTrail-User Header mit Username aus owltrail.conf.
Streaming: SSE / chunked Transfer-Encoding wird korrekt durchgeleitet.

Kein externe Abhängigkeiten (stdlib only).
"""

import io
import json
import logging
import os
import queue as _queue
import sys
import threading
import traceback
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_logger = logging.getLogger("owltrail")

_CONF_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "owltrail.conf")

_DEFAULTS = {
    "server_ip": "192.168.188.20",
    "quiteque_port": 7077,
    "listen_port": 8081,
    "username": "ella",
    "model_id": "auto",
    "timeout": 1800,
}


def load_conf(conf_file=None):
    path = conf_file or _CONF_FILE
    cfg = dict(_DEFAULTS)
    if os.path.exists(path):
        try:
            cfg.update(json.loads(open(path).read()))
        except Exception:
            pass
    return cfg


def _quiteque_base(cfg):
    """Gibt immer die QuiteQue-URL zurück — unabhängig von einem externen Backend."""
    return f"http://{cfg['server_ip']}:{cfg['quiteque_port']}"


def _backend_base(cfg):
    """Gibt die Backend-URL zurück.

    Priorität:
      1. cfg["backend_url"]  — explizit gesetzt (z.B. für OpenRouter)
      2. QuiteQue             — Standard
    """
    return cfg.get("backend_url") or _quiteque_base(cfg)


def _backend_key(cfg):
    """Gibt den API-Key für das Backend zurück (oder None für QuiteQue)."""
    return cfg.get("backend_key") or None


class _LogFileStream:
    """Schreibt alles was auf sys.stderr landet ins File-Logging (WARNING-Level)."""

    def write(self, msg):
        msg = msg.rstrip()
        if msg:
            _logger.warning("[stderr] %s", msg)

    def flush(self):
        pass

    def fileno(self):
        raise io.UnsupportedOperation("fileno")


def setup_logging(log_path, verbose=False):
    """Richtet dateibasiertes Logging ein — kein stdout/stderr, TUI bleibt sauber."""
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S"
    ))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    # http.server-Basisklasse loggt sonst auf stderr — abfangen
    logging.getLogger("http.server").setLevel(logging.WARNING)
    # stderr → direkt in die Log-Datei; erfasst auch socketserver-interne Abstürze
    # die jeden try/except-Block umgehen
    sys.stderr = open(log_path, "a", encoding="utf-8", buffering=1)


class _Handler(BaseHTTPRequestHandler):
    cfg = {}
    model_id_override = None

    def log_message(self, fmt, *args):
        _logger.debug(fmt, *args)

    def log_error(self, fmt, *args):
        _logger.error(fmt, *args)

    def _build_request(self, path, body):
        """Baut urllib.request.Request mit Auth-Headern und Modell-Override."""
        cfg     = self.__class__.cfg
        url     = _backend_base(cfg) + path
        username = cfg.get("username", "")
        api_key  = _backend_key(cfg) or "sk-no-key-required"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        if username:
            headers["X-User-ID"] = username
        if username and not cfg.get("backend_url"):
            headers["X-OwlTrail-User"] = username

        if body and self.__class__.model_id_override:
            try:
                data = json.loads(body)
                data["model"] = str(self.__class__.model_id_override)
                body = json.dumps(data).encode()
            except Exception:
                pass

        if body:
            headers["Content-Length"] = str(len(body))

        return urllib.request.Request(
            url, data=body, headers=headers,
            method="POST" if body else "GET",
        ), body

    def _proxy(self, path, body=None):
        cfg = self.__class__.cfg
        req, body = self._build_request(path, body)
        timeout   = int(cfg.get("timeout", 1800))

        # Heartbeat-Pfad: POST /v1/chat/completions
        if self.command == "POST" and path.rstrip("/").endswith("/chat/completions"):
            self._proxy_with_heartbeat(req, timeout, path)
            return

        # Alle anderen Endpunkte: direkt durchleiten
        resp = None
        try:
            resp      = urllib.request.urlopen(req, timeout=timeout)
            status    = resp.status
            ctype     = resp.headers.get("Content-Type", "application/json")
            is_stream = "text/event-stream" in ctype or "stream" in path

            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if is_stream:
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
            else:
                cl = resp.headers.get("Content-Length")
                if cl:
                    self.send_header("Content-Length", cl)
            self.end_headers()

            if is_stream:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode())
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
            else:
                try:
                    self.wfile.write(resp.read())
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        except urllib.error.HTTPError as e:
            body_err = e.read()
            _logger.error("HTTP %d vom Backend: %s — %s", e.code, path, body_err[:200])
            try:
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body_err)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        except Exception as e:
            _logger.error("Proxy-Fehler [%s]:\n%s", path, traceback.format_exc())
            msg = json.dumps({"error": {"message": str(e), "type": "proxy_error"}}).encode()
            try:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(msg)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

    def _proxy_with_heartbeat(self, req, timeout, path):
        """POST /v1/chat/completions mit Heartbeat-SSE.

        Reihenfolge (wichtig!):
          1. 200 + SSE-Header sofort an opencode — Verbindung bleibt offen
          2. Backend-Request im Hintergrund-Thread starten
          3. Alle 15 s ': heartbeat' senden bis erste Daten kommen
          4. Echte Chunks durchleiten, Stream sauber schließen
        """
        # ── 1. Header ZUERST senden — noch bevor der Backend-Request startet ──
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.flush()
        except OSError as e:
            _logger.error("Header senden fehlgeschlagen: %s", e)
            return

        _logger.info("Heartbeat-Stream geöffnet: %s", path)

        # ── 2. Backend-Fetch im eigenen Thread ────────────────────────────────
        chunk_q = _queue.Queue()
        DONE    = object()

        def _fetch():
            resp = None
            try:
                resp = urllib.request.urlopen(req, timeout=timeout)
                _logger.info("Backend antwortet (Status %d): %s", resp.status, path)
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    chunk_q.put(chunk)
            except urllib.error.HTTPError as e:
                body_err = e.read()
                _logger.error("HTTP %d vom Backend: %s — %s", e.code, path, body_err[:200])
                chunk_q.put(
                    f"data: {json.dumps({'error': {'message': body_err.decode(errors='replace'), 'type': 'http_error', 'code': e.code}})}\n\n".encode()
                )
            except Exception:
                _logger.error("Backend-Fehler [%s]:\n%s", path, traceback.format_exc())
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
                chunk_q.put(DONE)

        threading.Thread(target=_fetch, daemon=True).start()

        # ── 3 & 4. Heartbeat-Schleife + Chunks weiterleiten ──────────────────
        def _send(data):
            """Gibt True zurück wenn erfolgreich, False bei Disconnect (still)."""
            try:
                self.wfile.write(f"{len(data):X}\r\n".encode())
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                _logger.debug(
                    "Client (opencode) hat die Verbindung unterbrochen. Thread beendet."
                )
                return False

        # Echter SSE-data-Event (kein Kommentar) → OpenClaw resettet Idle-Timer
        HEARTBEAT  = b'data: {"id":"hb","object":"chat.completion.chunk","created":0,"model":"heartbeat","choices":[{"index":0,"delta":{},"finish_reason":null}]}\n\n'
        got_first  = False
        beat_count = 0

        while True:
            try:
                chunk = chunk_q.get(timeout=15)
            except _queue.Empty:
                beat_count += 1
                _logger.debug("Heartbeat #%d → Client (%s)", beat_count, path)
                if not _send(HEARTBEAT):
                    break
                continue

            if chunk is DONE:
                break
            if not got_first:
                _logger.info("Erste Backend-Daten für %s", path)
                got_first = True
            if not _send(chunk):
                break

        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        _logger.info("Heartbeat-Stream geschlossen (%s, %d Beats)", path, beat_count)

    def do_GET(self):
        self._proxy(self.path)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None
        self._proxy(self.path, body)


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer mit stummem ConnectionReset-Handler.

    socketserver.BaseServer.handle_error() druckt standardmäßig den vollen
    Traceback auf stderr. Wir fangen ConnectionResetError / BrokenPipeError
    (opencode öffnet Keep-Alive-Verbindungen und resettet sie sofort) stumm ab
    und loggen nur eine einzige DEBUG-Zeile.
    """

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            _logger.debug(
                "Client (opencode) hat die Verbindung unterbrochen. Thread beendet."
            )
        else:
            _logger.error(
                "Unerwarteter Server-Fehler von %s:\n%s",
                client_address,
                traceback.format_exc(),
            )


class OwlTrailProxy:
    """Lokaler OpenAI-kompatibler Proxy → QuiteQue."""

    def __init__(self, conf_file=None, model_id=None):
        self.cfg = load_conf(conf_file)
        self.model_id = model_id
        self._port = int(self.cfg["listen_port"])
        self._server = None
        self._thread = None

    @property
    def port(self):
        return self._port

    def start(self, port=8081):
        self._port = port
        _Handler.cfg = self.cfg
        _Handler.model_id_override = self.model_id
        self._server = _Server(("127.0.0.1", port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self._port

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
            except (KeyboardInterrupt, OSError, Exception):
                pass
            self._server = None

    def set_model(self, model_id):
        self.model_id = model_id
        _Handler.model_id_override = model_id

    @property
    def username(self):
        return self.cfg.get("username", "")
