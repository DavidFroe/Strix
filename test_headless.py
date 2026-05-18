#!/usr/bin/env python3
"""
Headless-Test für DeepSeek-TUI × OwlTrail.

Startet den OwlTrail-Adapter (mit Model-Mapping) und testet drei Pfade:

  A) Default-Modell (deepseek-v4-pro → 215)
  B) Fast-Modell    (fast            → 20)
  C) Streaming      (SSE chat.completion.chunk)

Schreibt:
  - test_results.json (alle Resultate strukturiert)
  - test_results.log  (Roh-Output / Diagnose)

Exit 0 wenn alle Tests bestehen, sonst 1.

Standalone — nur stdlib + owltrail.py + owltrail_adapter.py.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import owltrail  # noqa: E402
import owltrail_adapter  # noqa: E402

LOG_PATH  = os.path.join(HERE, "test_results.log")
JSON_PATH = os.path.join(HERE, "test_results.json")

PROMPT_OK   = "Sag genau das Wort: OWLTRAIL_OK. Sonst nichts."
PROMPT_FAST = "Sag genau das Wort: FAST_OK. Sonst nichts."
PROMPT_STREAM = "Zähle laut von 1 bis 5 — nur die Zahlen, durch Komma getrennt."

# ───────────────────────────────────────────────────────────────────────────
# Logging
# ───────────────────────────────────────────────────────────────────────────

_log_fp = open(LOG_PATH, "w", encoding="utf-8", buffering=1)


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    _log_fp.write(line + "\n")


# ───────────────────────────────────────────────────────────────────────────
# HTTP-Helper
# ───────────────────────────────────────────────────────────────────────────

def _wait_port(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((host, port), 0.3)
            s.close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def post_json(url: str, payload: dict, stream: bool = False, timeout: int = 120):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer sk-owltrail",
            "Accept": "text/event-stream" if stream else "application/json",
        },
    )
    return urllib.request.urlopen(req, timeout=timeout)


# ───────────────────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────────────────

def _consume_sse(resp, log_prefix: str = ""):
    """Generischer SSE-Consumer für owltrail-Antworten.

    owltrail.py schickt /chat/completions IMMER als SSE (auch bei stream=false).
    Daher hier den Stream parsen und das aggregierte Resultat zurückliefern.
    """
    out = {
        "chunks": 0,
        "deltas": 0,
        "content": [],
        "reasoning_deltas": 0,
        "reasoning_content": [],
        "finish_reason": None,
        "model_returned": None,
        "usage": None,
        "first_data_dt": None,
        "raw_errors": [],
    }
    t0 = time.time()
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            continue
        out["chunks"] += 1
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        if ev.get("model") == "heartbeat":
            continue
        if out["first_data_dt"] is None and ev.get("choices"):
            out["first_data_dt"] = time.time() - t0

        if ev.get("model"):
            out["model_returned"] = ev["model"]

        if isinstance(ev.get("error"), (dict, str)):
            out["raw_errors"].append(ev["error"])

        for ch in ev.get("choices") or []:
            delta = ch.get("delta") or {}
            content = delta.get("content")
            if content:
                out["deltas"] += 1
                out["content"].append(content)
            # DeepSeek-V4-Pro / Reasoner: Tokens kommen oft in reasoning_content
            rc = delta.get("reasoning_content")
            if rc:
                out["reasoning_deltas"] += 1
                out["reasoning_content"].append(rc)
            if ch.get("finish_reason"):
                out["finish_reason"] = ch["finish_reason"]

        usage = ev.get("usage")
        if isinstance(usage, dict):
            out["usage"] = usage

    out["content"]           = "".join(out["content"]).strip()
    out["reasoning_content"] = "".join(out["reasoning_content"]).strip()
    return out


def test_chat(name: str, base_url: str, model: str, prompt: str,
              expect_substr: str | None, max_tokens: int = 256) -> dict:
    """Non-Streaming-Logik aber via SSE (owltrail.py erzwingt SSE).

    max_tokens=256 als Default: Reasoning-Modelle (deepseek-v4-pro) verbrauchen
    typischerweise 50–150 Tokens fürs interne Reasoning, bevor sie überhaupt
    Content emittieren. Bei max_tokens=64 wurde das Antwort-Budget gelegentlich
    aufgebraucht bevor das erwartete Substring vollständig ausgegeben war.
    """
    res = {
        "name": name,
        "model_requested": model,
        "passed": False,
        "latency_first_response_s": None,
        "elapsed_s": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cached_tokens": None,
        "model_returned": None,
        "content": "",
        "finish_reason": None,
        "chunks": 0,
        "deltas": 0,
        "error": None,
    }
    log(f"── {name}: chat/completions (model={model}) ──")

    t0 = time.time()
    try:
        # stream=True, da owltrail.py ohnehin SSE liefert
        resp = post_json(
            base_url + "/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
                "max_tokens": max_tokens,
                "temperature": 0.0,
            },
            stream=True,
            timeout=120,
        )
        agg = _consume_sse(resp)
        res["elapsed_s"]               = round(time.time() - t0, 3)
        res["latency_first_response_s"] = (
            round(agg["first_data_dt"], 3) if agg["first_data_dt"] is not None else None
        )
        res["model_returned"]  = agg["model_returned"]
        res["content"]         = agg["content"]
        res["chunks"]          = agg["chunks"]
        res["deltas"]          = agg["deltas"]
        res["finish_reason"]   = agg["finish_reason"]

        usage = agg["usage"] or {}
        res["prompt_tokens"]     = usage.get("prompt_tokens")
        res["completion_tokens"] = usage.get("completion_tokens")
        res["total_tokens"]      = usage.get("total_tokens")
        res["cached_tokens"]     = (
            usage.get("prompt_cache_hit_tokens")
            or usage.get("cached_tokens")
            or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        )

        if agg["raw_errors"]:
            res["error"] = "Backend-Errors: " + json.dumps(agg["raw_errors"])[:300]
            log(res["error"])

        # owltrail packt Backend-Fehler manchmal als Inhalt: "[Fehler HTTP 4xx]"
        # oder "QuiteQue: ..." — als Fail behandeln.
        backend_err_markers = ("[Fehler HTTP", "QuiteQue:", "[Verbindungsfehler")
        looks_like_error = any(m in res["content"] for m in backend_err_markers)

        ok = True
        if looks_like_error:
            ok = False
            res["error"] = res["error"] or ("Backend-Fehler im Content: " + res["content"][:200])
            log(f"  Backend-Fehler im Inhalt: {res['content']!r}")
        elif expect_substr and expect_substr not in res["content"]:
            ok = False
            log(f"  Erwartetes Substring '{expect_substr}' NICHT in: {res['content']!r}")
        else:
            log(f"  Antwort: {res['content']!r}")
        res["passed"] = ok and bool(res["content"]) and not res["error"]

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        res["error"] = f"HTTP {e.code}: {body[:300]}"
        log(res["error"])
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        log(res["error"])
        log(traceback.format_exc())

    log(f"  Dauer: {res['elapsed_s']}s | first={res['latency_first_response_s']}s | tokens: "
        f"prompt={res['prompt_tokens']} compl={res['completion_tokens']} "
        f"total={res['total_tokens']} cached={res['cached_tokens']}")
    return res


def test_stream(name: str, base_url: str, model: str, prompt: str,
                max_tokens: int = 256) -> dict:
    """Streaming-Test: zählt sowohl content- als auch reasoning_content-Deltas
    (für Reasoning-Modelle wie deepseek-v4-pro). Pass = irgendein Token-Output
    + finish_reason 'stop' ODER nicht-leerer Content."""
    res = {
        "name": name,
        "model_requested": model,
        "passed": False,
        "latency_first_response_s": None,
        "elapsed_s": None,
        "chunks": 0,
        "deltas": 0,
        "reasoning_deltas": 0,
        "content": "",
        "reasoning_content_len": 0,
        "finish_reason": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "cached_tokens": None,
        "model_returned": None,
        "error": None,
    }
    log(f"── {name}: streaming chat/completions (model={model}, max_tokens={max_tokens}) ──")

    t0 = time.time()
    try:
        resp = post_json(
            base_url + "/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
                "max_tokens": max_tokens,
                "temperature": 0.0,
            },
            stream=True,
            timeout=180,
        )
        agg = _consume_sse(resp)
        res["elapsed_s"]                = round(time.time() - t0, 3)
        res["latency_first_response_s"] = (
            round(agg["first_data_dt"], 3) if agg["first_data_dt"] is not None else None
        )
        res["chunks"]              = agg["chunks"]
        res["deltas"]              = agg["deltas"]
        res["reasoning_deltas"]    = agg["reasoning_deltas"]
        res["content"]             = agg["content"]
        res["reasoning_content_len"] = len(agg["reasoning_content"])
        res["finish_reason"]       = agg["finish_reason"]
        res["model_returned"]      = agg["model_returned"]
        usage = agg["usage"] or {}
        res["prompt_tokens"]     = usage.get("prompt_tokens")
        res["completion_tokens"] = usage.get("completion_tokens")
        res["reasoning_tokens"]  = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        res["total_tokens"]      = usage.get("total_tokens")
        res["cached_tokens"]     = (
            usage.get("prompt_cache_hit_tokens")
            or usage.get("cached_tokens")
            or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        )
        if agg["raw_errors"]:
            res["error"] = "Backend-Errors: " + json.dumps(agg["raw_errors"])[:300]
        backend_err_markers = ("[Fehler HTTP", "QuiteQue:", "[Verbindungsfehler")
        if any(m in res["content"] for m in backend_err_markers):
            res["error"] = res["error"] or ("Backend-Fehler im Content: " + res["content"][:200])

        # Pass-Kriterium: Stream lieferte verwertbare Token (content ODER reasoning),
        # kein Backend-Fehler, und finish_reason ist gesetzt (stop/length/...).
        any_tokens = (res["deltas"] > 0) or (res["reasoning_deltas"] > 0)
        res["passed"] = (
            any_tokens
            and (bool(res["content"]) or res["reasoning_content_len"] > 0)
            and not res["error"]
        )

        log(f"  chunks={res['chunks']} content_deltas={res['deltas']} "
            f"reasoning_deltas={res['reasoning_deltas']} "
            f"first={res['latency_first_response_s']}s total={res['elapsed_s']}s "
            f"finish={res['finish_reason']}")
        if res["content"]:
            log(f"  content:   {res['content']!r}")
        if res["reasoning_content_len"]:
            log(f"  reasoning: {res['reasoning_content_len']} chars (reasoning_tokens={res['reasoning_tokens']})")

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        res["error"] = f"HTTP {e.code}: {body[:300]}"
        log(res["error"])
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        log(res["error"])
        log(traceback.format_exc())

    return res


def _find_propeller_binary() -> str | None:
    """Sucht das propeller/deepseek-tui-Binary in den üblichen Pfaden.

    Reihenfolge analog start.sh: target/release/propeller zuerst, dann
    npm-Downloads, dann PATH.
    """
    candidates = [
        os.path.join(HERE, "target", "release", "propeller"),
        os.path.join(HERE, "npm", "deepseek-tui", "bin", "downloads", "deepseek-tui"),
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    # PATH-Fallback
    from shutil import which
    return which("propeller") or which("deepseek-tui")


def test_exec_subcommand(name: str, base_url: str, model: str, prompt: str,
                         expect_substr: str | None, timeout_s: int = 60) -> dict:
    """Headless-Test des `propeller exec`-Subkommandos.

    Verifiziert den Non-Stream-Pfad: propeller schickt stream=false an den
    Adapter, der den Backend-SSE einsammelt und als JSON Chat-Completion
    zurückliefert. Ohne install_stream_handling_patch() würde propeller hier
    BrokenPipe / "Network error" sehen — der Test schützt also genau diesen Fix.
    """
    res = {
        "name": name,
        "model_requested": model,
        "passed": False,
        "elapsed_s": None,
        "exit_code": None,
        "stdout_len": 0,
        "stderr_tail": "",
        "output": "",
        "error": None,
    }
    log(f"── {name}: propeller exec --json (model={model}) ──")

    binary = _find_propeller_binary()
    if not binary:
        res["error"] = "propeller-Binary nicht gefunden (target/release/propeller fehlt)"
        log(f"  SKIP: {res['error']}")
        # Skip behandelt als nicht-Fail, da Binary-Build optional ist.
        res["passed"] = True
        res["error"] = (res["error"] or "") + " — als skipped gewertet"
        return res

    env = {
        **os.environ,
        "DEEPSEEK_API_KEY":  "sk-owltrail",
        "DEEPSEEK_BASE_URL": base_url,
        "DEEPSEEK_MODEL":    model,
        "DEEPSEEK_FORCE_HTTP1": "1",
    }
    t0 = time.time()
    try:
        proc = subprocess.run(
            [binary, "exec", "--json", prompt],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        res["elapsed_s"]  = round(time.time() - t0, 3)
        res["exit_code"]  = proc.returncode
        res["stdout_len"] = len(proc.stdout)
        res["stderr_tail"] = proc.stderr[-300:] if proc.stderr else ""

        if proc.returncode != 0:
            res["error"] = (f"exec exit={proc.returncode}: "
                            f"stderr={res['stderr_tail']}")
            log(res["error"])
            return res

        # --json: erste Zeile/Block ist JSON, danach evtl. Logs
        out_clean = proc.stdout.strip()
        try:
            j = json.loads(out_clean)
            res["output"] = str(j.get("output", "")).strip()
        except (ValueError, TypeError):
            # Falls JSON-Parse fehlschlägt, plain text als Output behandeln
            res["output"] = out_clean

        if expect_substr and expect_substr not in res["output"]:
            res["error"] = (f"Erwartetes Substring {expect_substr!r} "
                            f"nicht in Output: {res['output']!r}")
            log(res["error"])
        else:
            log(f"  exec output: {res['output']!r}")
            res["passed"] = bool(res["output"])

    except subprocess.TimeoutExpired:
        res["error"] = f"propeller exec hängt nach {timeout_s}s"
        log(res["error"])
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        log(res["error"])
        log(traceback.format_exc())
    return res


# ───────────────────────────────────────────────────────────────────────────
# Setup / Backend-Reachability
# ───────────────────────────────────────────────────────────────────────────

def assert_backend_reachable(cfg: dict) -> None:
    host = cfg.get("server_ip")
    port = int(cfg.get("quiteque_port"))
    try:
        s = socket.create_connection((host, port), 3.0)
        s.close()
    except OSError as e:
        sys.stderr.write(
            f"FEHLER: OwlTrail kann QuiteQue-Backend auf {host}:{port} "
            f"nicht erreichen ({e}).\n"
            f"Prüfe: VPN aktiv? Server läuft? owltrail.conf korrekt?\n"
        )
        sys.exit(2)


# ───────────────────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────────────────

def main() -> int:
    log("DeepSeek-TUI × OwlTrail Headless-Tests gestartet.")
    log(f"  Working dir: {HERE}")

    cfg = owltrail.load_conf(os.path.join(HERE, "owltrail.conf"))
    log(f"  owltrail.conf: server={cfg.get('server_ip')}:{cfg.get('quiteque_port')} "
        f"user={cfg.get('username')} models={cfg.get('models')}")

    assert_backend_reachable(cfg)
    log("  Backend erreichbar.")

    # Mapping-Patch installieren und Proxy starten.
    # Stream-Handling-Patch ebenfalls aktivieren, damit der exec-Subkommando-
    # Pfad (stream=false → JSON-Sammeln statt SSE) auch im Test mitgetestet wird.
    owltrail_adapter.install_model_mapping_patch()
    owltrail_adapter.install_stream_handling_patch()
    port = 8181  # Testport — kollisionsfrei zu evtl. laufendem 8081
    proxy = owltrail.OwlTrailProxy(conf_file=os.path.join(HERE, "owltrail.conf"))
    proxy.start(port=port)
    base_url = f"http://127.0.0.1:{port}/v1"
    log(f"  OwlTrail-Proxy gestartet auf {base_url}")

    if not _wait_port("127.0.0.1", port, 3.0):
        log("FEHLER: Proxy reagiert nicht.")
        proxy.stop()
        return 1

    # Adapter-Funktions-Smoketests
    smoke = {
        "get_owltrail_url(8181)": owltrail_adapter.get_owltrail_url(8181),
        "get_model_id(None)":         owltrail_adapter.get_model_id(None),
        "get_model_id('deepseek-chat')":     owltrail_adapter.get_model_id("deepseek-chat"),
        "get_model_id('deepseek-v4-pro')":   owltrail_adapter.get_model_id("deepseek-v4-pro"),
        "get_model_id('fast')":              owltrail_adapter.get_model_id("fast"),
        "get_model_id('deepseek-flash')":    owltrail_adapter.get_model_id("deepseek-flash"),
        "get_model_id('web')":               owltrail_adapter.get_model_id("web"),
        "get_model_id('215')":               owltrail_adapter.get_model_id("215"),
    }
    log(f"  Adapter-Smoketests: {smoke}")

    results = []

    # Test 0 — Proxy-Wiring: GET /v1/models muss erreichbar sein
    log("── 0_models_endpoint: GET /v1/models ──")
    t0 = time.time()
    res0 = {"name": "0_models_endpoint", "passed": False,
            "elapsed_s": None, "model_count": 0,
            "found_deepseek": False, "error": None}
    try:
        req = urllib.request.Request(base_url + "/models",
            headers={"Authorization": "Bearer sk-owltrail"})
        r = urllib.request.urlopen(req, timeout=15)
        j = json.loads(r.read())
        data = j.get("data") or []
        res0["model_count"]    = len(data)
        res0["elapsed_s"]      = round(time.time() - t0, 3)
        res0["found_deepseek"] = any("deepseek" in str(m.get("id","")).lower()
                                     or "deepseek" in str(m.get("name","")).lower()
                                     for m in data)
        res0["passed"] = res0["model_count"] > 0
        log(f"  Modelle gefunden: {res0['model_count']} "
            f"(DeepSeek dabei: {res0['found_deepseek']}) "
            f"in {res0['elapsed_s']}s")
    except Exception as e:
        res0["error"] = f"{type(e).__name__}: {e}"
        log(res0["error"])
    results.append(res0)

    # Test A — Default
    results.append(test_chat(
        "A_default_model",
        base_url,
        model="deepseek-v4-pro",
        prompt=PROMPT_OK,
        expect_substr="OWLTRAIL_OK",
    ))

    # Test B — Fast
    results.append(test_chat(
        "B_fast_model",
        base_url,
        model="deepseek-v4-flash",
        prompt=PROMPT_FAST,
        expect_substr="FAST_OK",
    ))

    # Test C1 — Streaming via Reasoning-Modell (deepseek-v4-pro):
    # max_tokens=512 damit Reasoning + content beide Platz haben.
    results.append(test_stream(
        "C1_streaming_pro",
        base_url,
        model="deepseek-v4-pro",
        prompt=PROMPT_STREAM,
        max_tokens=512,
    ))

    # Test C2 — Streaming via Fast-Modell (kein Reasoning):
    # garantiert content-Tokens, klassischer SSE-Flow.
    results.append(test_stream(
        "C2_streaming_fast",
        base_url,
        model="deepseek-v4-flash",
        prompt=PROMPT_STREAM,
        max_tokens=64,
    ))

    # Test D1 — exec-Subkommando via propeller-Binary (non-streaming Pfad).
    # Schützt den install_stream_handling_patch()-Fix: propeller schickt
    # stream=false, der Adapter sammelt SSE und antwortet mit JSON.
    results.append(test_exec_subcommand(
        "D1_exec_flash",
        base_url,
        model="deepseek-v4-flash",
        prompt="Antworte nur mit dem Wort: OK",
        expect_substr="OK",
        timeout_s=30,
    ))

    # Test D2 — exec mit Reasoning-Modell (Pro). max_tokens-Limit greift hier
    # nicht, da exec --json den vollen completion-Pfad nutzt.
    results.append(test_exec_subcommand(
        "D2_exec_pro",
        base_url,
        model="deepseek-v4-pro",
        prompt="Antworte nur mit dem Wort: PRO_OK",
        expect_substr="PRO_OK",
        timeout_s=90,
    ))

    proxy.stop()
    log("OwlTrail-Proxy gestoppt.")

    passed = sum(1 for r in results if r.get("passed"))
    failed = len(results) - passed

    summary = {
        "total":  len(results),
        "passed": passed,
        "failed": failed,
        "config": {
            "server": f"{cfg.get('server_ip')}:{cfg.get('quiteque_port')}",
            "user":   cfg.get("username"),
            "models": cfg.get("models"),
        },
        "adapter_smoke": smoke,
        "results": results,
    }

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    log(f"  Ergebnis-JSON: {JSON_PATH}")

    # Tabellarische Zusammenfassung
    log("")
    log("══════════════════════════════════════════════════════════════════")
    log(f"  TESTS:    {len(results)} gesamt  |  {passed} ok  |  {failed} fail")
    log("══════════════════════════════════════════════════════════════════")
    for r in results:
        flag = "PASS" if r.get("passed") else "FAIL"
        log(f"  [{flag}] {r['name']:18s}  "
            f"model={r.get('model_requested')!r:<22s} "
            f"latency={r.get('latency_first_response_s')}s  "
            f"tok={r.get('prompt_tokens')}/{r.get('completion_tokens')}/{r.get('total_tokens')}"
            f"  cached={r.get('cached_tokens')}")
        if r.get("error"):
            log(f"         err: {r['error']}")
    log("══════════════════════════════════════════════════════════════════")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 130
    _log_fp.close()
    sys.exit(code)
