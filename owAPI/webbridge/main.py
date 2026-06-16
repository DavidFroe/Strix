# webbridge/main.py
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from webbridge.config import BridgeConfig
from webbridge.coords import build_abs_coords, build_rel_coords
from webbridge.runner import (
    start_chrome_debug,
    connect_chrome,
    pick_or_open_chat_page,
    ensure_composer_or_recover,
    enable_temporary_chat,
    enable_web_search,
    mouse_fallback_enable_web_search,  # CSS-Pixel-Fallback (Playwright-Maus)
    send_prompt,
    wait_for_json_codeblock,
    on_login_page,
    log,
)




def _extract_wait_settings(cfg, wait_conf: dict):
    """Liefert (expect_json, max_wait, settle_time) mit sinnvollen Defaults,
    ohne dass cfg.json_expect_codeblock existieren muss."""
    json_total_timeout_s = getattr(cfg, "json_total_timeout_s", 120)
    json_settle_s = getattr(cfg, "json_settle_s", 0.6)

    expect_json = bool(wait_conf.get("expect_json_codeblock", True))
    max_wait = int(wait_conf.get("max_wait_s", json_total_timeout_s))
    settle_time = float(wait_conf.get("settle_s", json_settle_s))
    return expect_json, max_wait, settle_time



def main():
    t0 = time.perf_counter()
    timings = {}

    # Load prompt.json
    t = time.perf_counter()
    prompt_file = Path("prompt.json")
    if not prompt_file.is_file():
        print("Prompt file prompt.json not found.", file=sys.stderr)
        sys.exit(13)
    try:
        prompt_data = json.loads(prompt_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error reading prompt.json: {e}", file=sys.stderr)
        sys.exit(13)
    timings["read_prompt_ms"] = int((time.perf_counter() - t) * 1000)

    # Validate minimal structure
    if "message" not in prompt_data or "conversation" not in prompt_data or "ui" not in prompt_data:
        print("Invalid prompt.json structure.", file=sys.stderr)
        sys.exit(13)

    # Prepare config and parameters
    cfg = BridgeConfig()

    # Set conversation flags from prompt
    conv = prompt_data.get("conversation", {})
    cfg.use_temporary_chat = bool(conv.get("temporary_chat", False))
    cfg.use_web_search = bool(conv.get("enable_web_search", False))
    new_chat = bool(conv.get("new_chat", True))

    # UI coordinates from prompt (absolute + relative)
    ui = prompt_data.get("ui", {}) or {}
    cfg.dom_first = bool(ui.get("dom_first", True))
    abs_ui = ui.get("absolute", {}) or {}
    rel_ui = ui.get("relative", {}) or {}

    # Build coordinate maps
    abs_coords = build_abs_coords(cfg, abs_ui)      # dict mit keys: composer, plus, more, web (CSS- oder Screen-Pixel je nach Modus)
    rel_coords = build_rel_coords(rel_ui)           # dict mit keys: composer, plus, more, web (jeweils 0..1)

    # Optional: Modus steuern (rel | px | screen). Default: rel
    abs_mode = str(ui.get("abs_mode", "rel")).lower().strip()
    if abs_mode not in ("rel", "px", "screen"):
        abs_mode = "rel"
    log(f"[bridge] ABS mode = {abs_mode}")

    # Falls absolute Punkte explizit übergeben wurden, auch die cfg-Defaults überschreiben,
    # so dass andere Stellen (z. B. mouse_focus_composer) davon profitieren können.
    try:
        if "composer_xy" in abs_ui:
            cfg.mouse_composer_xy = tuple(abs_ui["composer_xy"])
        if "plus_xy" in abs_ui:
            cfg.mouse_plus_xy = tuple(abs_ui["plus_xy"])
        if "more_xy" in abs_ui:
            cfg.mouse_more_hover_xy = tuple(abs_ui["more_xy"])
        if "web_xy" in abs_ui:
            cfg.mouse_web_xy_regular = tuple(abs_ui["web_xy"])
    except Exception:
        pass  # bei Formatfehlern einfach Defaults lassen

# Wait settings (robust, ohne cfg.json_expect_codeblock)
    wait_conf = prompt_data.get("wait", {}) or {}
    expect_json, max_wait, settle_time = _extract_wait_settings(cfg, wait_conf)


    # The user prompt message and meta
    user_message = prompt_data.get("message", "")
    meta = prompt_data.get("meta", {}) or {}
    task_id = meta.get("task_id", "")

    # System prompt (deterministic JSON controller instructions)
    system_prompt = (
        "Du agierst als deterministischer JSON-Controller zwischen WebBridge, Application und ownAPI.\n"
        "REGELN:\n"
        "- Antworte IMMER mit GENAU EINEM gültigen JSON-Codeblock (```json … ```), ohne Freitext.\n"
        "- Output-Schema v1:\n"
        "  {\n"
        "    \"decision\": \"finish\" | \"produce_update_json\" | \"ask_followup\" | \"retry\",\n"
        "    \"message\": \"kurz deutsch\",\n"
        "    \"web_findings\": [ { \"title\": \"...\", \"url\": \"https://...\", \"notes\": \"...\" } ],\n"
        "    \"update_json\": {},\n"
        "    \"next_action\": { \"open_new_chat\": false, \"recommend_web_search\": false, \"recommend_temporary_chat\": false },\n"
        "    \"hints\": { \"target_row\": null, \"target_header\": null, \"alt_urls\": [] }\n"
        "  }\n"
        "- Nutze Web-Quellen knapp & verifizierbar (keine erfundenen URLs).\n"
        "- Wenn du `status.json` bekommst, analysiere & liefere direkt eine korrigierte `update_json` (decision=\"produce_update_json\").\n"
    )
    full_message = f"{system_prompt}\n{user_message}"

    # Prepare events logging
    events = []
    errors = []

    exit_code = 0
    browser = None
    try:
        with sync_playwright() as pw:
            # Connect Chrome
            t = time.perf_counter()
            try:
                browser = connect_chrome(pw, cfg)
                timings["cdp_connect_ms"] = int((time.perf_counter() - t) * 1000)
            except Exception:
                t_retry = time.perf_counter()
                try:
                    start_chrome_debug(cfg)
                    timings["chrome_boot_ms"] = int((time.perf_counter() - t_retry) * 1000)
                    t2 = time.perf_counter()
                    browser = connect_chrome(pw, cfg)
                    timings["cdp_connect_ms"] = int((time.perf_counter() - t2) * 1000)
                except Exception as e:
                    log(f"[bridge] ❌ Chrome/Playwright-Fehler: {e}")
                    errors.append(str(e))
                    sys.exit(13)

            # Open chat page
            t = time.perf_counter()
            try:
                page = pick_or_open_chat_page(browser, cfg.chat_url)
                timings["open_chat_ms"] = int((time.perf_counter() - t) * 1000)
            except Exception as e:
                log(f"[bridge] ❌ Browser-Seite nicht verfügbar: {e}")
                errors.append("Browser page open failed")
                sys.exit(13)

            # Ensure composer
            t = time.perf_counter()
            success = ensure_composer_or_recover(page, cfg, new_chat=new_chat)
            timings["ensure_composer_ms"] = int((time.perf_counter() - t) * 1000)
            if not success:
                if on_login_page(page):
                    log("[bridge] ⚠️ Login nötig. Abbruch.")
                    errors.append("Login required")
                    exit_code = 12
                else:
                    log("[bridge] ⚠️ Composer nicht verfügbar. Abbruch.")
                    errors.append("Composer not visible")
                    exit_code = 13
                resp = {
                    "protocol_version": "1.0.0",
                    "status": "error",
                    "data": {},
                    "raw": {"text_excerpt": "", "json_blocks_found": 0, "timings": timings},
                    "events": [],
                    "errors": errors
                }
                Path("response.json").write_text(json.dumps(resp, ensure_ascii=False, indent=2), encoding="utf-8")
                sys.exit(exit_code if exit_code != 0 else 13)

            events.append({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "e": "composer_focused"})

            # Temporary chat
            if cfg.use_temporary_chat:
                t = time.perf_counter()
                if enable_temporary_chat(page):
                    events.append({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "e": "temporary_chat_enabled"})
                timings["enable_tempchat_ms"] = int((time.perf_counter() - t) * 1000)

            if cfg.use_web_search:
                t = time.perf_counter()
                web_enabled = False
            
                if cfg.dom_first:
                    web_enabled = enable_web_search(page, cfg)
                    timings["websearch_dom_ms"] = int((time.perf_counter() - t) * 1000)
                    if not web_enabled:
                        log("[bridge] Websuche via DOM nicht aktivierbar – Maus-Fallback…")
                        t2 = time.perf_counter()
                        mode = "rel" if rel_coords else "px"
                        print(f"[bridge] ABS mode = {mode}", flush=True)
                        web_enabled = mouse_fallback_enable_web_search(
                            page, cfg, retries=2, mode=mode,
                            abs_coords=abs_coords, rel_coords=rel_coords
                        )
                        timings["websearch_mouse_ms"] = int((time.perf_counter() - t2) * 1000)
                else:
                    t2 = time.perf_counter()
                    mode = "rel" if rel_coords else "px"
                    print(f"[bridge] ABS mode = {mode}", flush=True)
                    web_enabled = mouse_fallback_enable_web_search(
                        page, cfg, retries=2, mode=mode,
                        abs_coords=abs_coords, rel_coords=rel_coords
                    )
                    timings["websearch_mouse_ms"] = int((time.perf_counter() - t2) * 1000)
            
                if web_enabled:
                    log("[bridge] 🌐 Web search aktiviert.")
                    events.append({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "e": "web_search_enabled"})
                else:
                    log("[bridge] ⚠️ Websuche konnte nicht aktiviert werden.")
            else:
                timings["websearch_skipped"] = True

            # Send the combined prompt
            t = time.perf_counter()
            send_prompt(page, cfg, full_message)
            timings["send_prompt_ms"] = int((time.perf_counter() - t) * 1000)
            events.append({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "e": "prompt_sent"})

            # Wait for JSON codeblock
            t = time.perf_counter()
            result = None
            try:
                result = wait_for_json_codeblock(page, total_timeout_s=max_wait, settle_s=settle_time)
                timings["wait_json_ms"] = int((time.perf_counter() - t) * 1000)
            except Exception as e:
                timings["wait_json_ms"] = int((time.perf_counter() - t) * 1000)
                blocks_found = page.locator("pre").count()
                text_excerpt = ""
                if blocks_found > 0:
                    try:
                        text_excerpt = page.locator("pre").first.inner_text()[:400]
                    except Exception:
                        text_excerpt = ""
                    log("[bridge] ❌ Antwort nicht als gültiges JSON erhalten.")
                    exit_code = 10
                    errors.append("No valid JSON in response")
                else:
                    log("[bridge] ❌ Keine Antwort in der erwarteten Zeit erhalten.")
                    exit_code = 11
                    errors.append("Response timeout or no JSON code block")
                resp = {
                    "protocol_version": "1.0.0",
                    "status": "error",
                    "data": {},
                    "raw": {"text_excerpt": text_excerpt, "json_blocks_found": blocks_found, "timings": timings},
                    "events": events,
                    "errors": errors
                }
                Path("response.json").write_text(json.dumps(resp, ensure_ascii=False, indent=2), encoding="utf-8")
                sys.exit(exit_code)

            # If we got a result JSON dict
            raw_text_excerpt = ""
            try:
                raw_text_excerpt = page.locator("pre").last.inner_text()[:400]
            except Exception:
                raw_text_excerpt = ""
            json_blocks_found = page.locator("pre").count()
            timings["total_ms"] = int((time.perf_counter() - t0) * 1000)

            # Console timing summary
            def ms(k): return timings.get(k)
            print(
                f"[timings] connect={ms('cdp_connect_ms')}ms, open_chat={ms('open_chat_ms')}ms, "
                f"composer={ms('ensure_composer_ms')}ms, temp={ms('enable_tempchat_ms')}ms, "
                f"web_dom={ms('websearch_dom_ms')}ms, web_mouse={ms('websearch_mouse_ms')}ms, "
                f"send={ms('send_prompt_ms')}ms, wait_json={ms('wait_json_ms')}ms, total={ms('total_ms')}ms",
                flush=True
            )

            # Compose final response JSON
            resp = {
                "protocol_version": "1.0.0",
                "status": "ok",
                "data": result,
                "raw": {
                    "text_excerpt": raw_text_excerpt,
                    "json_blocks_found": int(json_blocks_found),
                    "timings": timings
                },
                "events": events,
                "errors": errors
            }
            Path("response.json").write_text(json.dumps(resp, ensure_ascii=False, indent=2), encoding="utf-8")
    except KeyboardInterrupt:
        print("Abbruch per KeyboardInterrupt.", file=sys.stderr)
        sys.exit(13)
    except Exception as e:
        log(f"[bridge] ❌ Unerwarteter Fehler: {e}")
        errors.append(str(e))
        resp = {
            "protocol_version": "1.0.0",
            "status": "error",
            "data": {},
            "raw": {"text_excerpt": "", "json_blocks_found": 0, "timings": timings},
            "events": events,
            "errors": errors
        }
        try:
            Path("response.json").write_text(json.dumps(resp, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        sys.exit(13)
    sys.exit(0)



if __name__ == "__main__":
    main()
