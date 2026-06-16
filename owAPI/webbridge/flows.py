# webbridge/flows.py  — vollständig
from __future__ import annotations
import time
from typing import Optional, Dict, Any, Callable

from .web_adapter import (
    wait_for_json_codeblock,
    ensure_composer_or_recover,
    detect_ui_error,
    click_try_again,
    soft_reload_chat,
    dismiss_memory_modal,
    web_search_is_active,   # Status-Check
    ws_begin_hold,          # ← Websearch bis Submit überwachen/re-aktivieren
    ws_end_hold,            # ← Hold beenden
)
from .search_ctrl import enable_once, sticky_verify_only
from .composer_io import focus_composer, feed_peck_check, insert_prompt, safe_submit


def _safe_submit_compat(page, expect_prefix: str) -> bool:
    """
    Verträglicher Wrapper für safe_submit() (versch. Signaturen).
    """
    try:
        return bool(safe_submit(page, expect_prefix=expect_prefix, allow_soft_fallback=True))
    except TypeError:
        try:
            return bool(safe_submit(page, expect_prefix=expect_prefix))
        except TypeError:
            return bool(safe_submit(page))


# ersetzen in File flows.py
# Funktion _verify_websearch_guard kann entfernt werden
def _verify_websearch_guard(page, cfg, latch_secs: float = 8.0, *, debug: bool = True) -> bool:
    """
    Kurzcheck direkt vor dem Prompt: ist Websuche aktiv? Wenn nein → einmal sauber (idempotent) aktivieren.
    """
    log = (lambda m: print(m, flush=True)) if debug else (lambda *_: None)
    try:
        if web_search_is_active(page):
            sticky_verify_only(page, cfg)
            log("[flow] websearch guard: still active → shield re-armed")
            return True
    except Exception:
        pass

    ok = enable_once(page, cfg, latch_secs=latch_secs)
    sticky_verify_only(page, cfg)
    log(f"[flow] websearch guard: re-enable attempt → {ok}")
    return ok


# ersetzen in File flows.py — vollständige Funktion
# Funktion _resend_same_chat_via_preamble kann entfernt werden
def _resend_same_chat_via_preamble(page, cfg, full_prompt: str, *, latch_secs: float = 8.0, debug: bool = True) -> bool:
    log = (lambda m: print(m, flush=True)) if debug else (lambda *_: None)
    pre = "Please generate a JSON file with the information of the following prompt:"

    ok = enable_once(page, cfg, latch_secs=latch_secs)
    sticky_verify_only(page, cfg)
    log(f"[flow] same-chat resend: enable_once → {ok}")

    # Hold aktiv bis Enter
    try:
        ws_begin_hold(page, ttl_s=max(15.0, latch_secs + 8.0), interval_ms=120)
    except Exception:
        pass

    try:
        # 1) preamble
        if focus_composer(page) == "fail":
            log("[flow] same-chat resend: focus fail (pre)")
            return False
        if not feed_peck_check(page):
            log("[flow] same-chat resend: feed-peck fail (pre)")
            return False
        if not insert_prompt(page, pre):
            log("[flow] same-chat resend: insert fail (pre)")
            return False
        if not _safe_submit_compat(page, pre[:80]):
            log("[flow] same-chat resend: submit abort (pre)")
            return False

        page.wait_for_timeout(900)

        # 2) main prompt
        if focus_composer(page) == "fail":
            log("[flow] same-chat resend: focus fail (main)")
            return False
        if not insert_prompt(page, full_prompt):
            log("[flow] same-chat resend: insert fail (main)")
            return False
        if not _safe_submit_compat(page, full_prompt[:80]):
            log("[flow] same-chat resend: submit abort (main)")
            return False

        log("[flow] same-chat resend: sent")
        return True
    finally:
        try:
            ws_end_hold(page)
        except Exception:
            pass


# ersetzen in File flows.py — vollständige Funktion
# Funktion _open_new_chat_and_resend kann entfernt werden
def _open_new_chat_and_resend(page, cfg, full_prompt: str, *, latch_secs: float = 8.0, debug: bool = True) -> bool:
    log = (lambda m: print(m, flush=True)) if debug else (lambda *_: None)
    if not ensure_composer_or_recover(page, cfg, new_chat=True):
        log("[flow] new-chat resend: composer not available")
        return False

    ok = enable_once(page, cfg, latch_secs=latch_secs)
    sticky_verify_only(page, cfg)
    log(f"[flow] new-chat resend: enable_once → {ok}")

    try:
        ws_begin_hold(page, ttl_s=max(15.0, latch_secs + 8.0), interval_ms=120)
    except Exception:
        pass

    try:
        if focus_composer(page) == "fail":
            log("[flow] new-chat resend: focus fail")
            return False
        if not feed_peck_check(page):
            log("[flow] new-chat resend: feed-peck fail")
            return False
        if not insert_prompt(page, full_prompt):
            log("[flow] new-chat resend: insert fail")
            return False
        if not _safe_submit_compat(page, full_prompt[:80]):
            log("[flow] new-chat resend: submit abort")
            return False

        log("[flow] new-chat resend: sent")
        return True
    finally:
        try:
            ws_end_hold(page)
        except Exception:
            pass


# ersetzen in File flows.py — vollständige Funktion
# Funktion send_prompt_with_websearch kann entfernt werden
def send_prompt_with_websearch(
    page,
    cfg,
    full_prompt: str,
    *,
    activation_latch_secs: float = 8.0,
    json_total_timeout_s: int = 75,
    json_settle_s: float = 0.6,
    debug: bool = True,
    startover_cb: Optional[Callable[[], bool]] = None,
    **_,
) -> Optional[Dict[str, Any]]:
    """
    Pipeline:
      1) Websuche EIN (idempotent + Latch)
      2) Fokus/Feed-Peck
      3) Prompt einfügen + Enter
      4) JSON abholen + Recovery
    """
    t0 = time.perf_counter()
    log = (lambda m: print(m, flush=True)) if debug else (lambda *_: None)

    # Safety: Memory-Modal weg
    try:
        dismiss_memory_modal(page, cfg)
    except Exception:
        pass

    # 1) Aktivieren + Shield/Sticky
    ok = enable_once(page, cfg, latch_secs=activation_latch_secs)
    log(f"[flow] websearch enable_once → {ok}")
    sticky_verify_only(page, cfg)
    log(f"[flow] sticky verify (no-toggle) → active={ok}")

    # 1b) Guard kurz vor Eingabe (falls zwischenzeitlich off)
    try:
        _verify_websearch_guard(page, cfg, latch_secs=activation_latch_secs, debug=debug)
    except Exception:
        pass

    # 1c) HOLD: Websuche bis zum Enter überwachen & ggf. sofort re-aktivieren
    try:
        ws_begin_hold(page, ttl_s=max(20.0, activation_latch_secs + 12.0), interval_ms=120)
    except Exception:
        pass

    try:
        # 2) Composer & Feed-Peck
        mode = focus_composer(page)
        log(f"[flow] focus_composer → {mode}")
        if mode == "fail":
            return None
        if not feed_peck_check(page):
            log("[flow] feed_peck_check → FAIL")
            return None
        log("[flow] feed_peck_check → OK")
        breakpoint() 
        # 3) Prompt eingeben & abschicken
        if not insert_prompt(page, full_prompt):
            log("[flow] insert_prompt → FAIL")
            return None
        if not _safe_submit_compat(page, full_prompt[:80]):
            log("[flow] safe_submit → ABORT (composer mismatch)")
            return None
        log("[flow] safe_submit → Enter sent")
    finally:
        # HOLD beenden, egal ob Erfolg/Abbruch
        try:
            ws_end_hold(page)
        except Exception:
            pass

    # 4) JSON abholen (+ Recovery)
    deadline = time.time() + float(json_total_timeout_s)
    tried_same = False
    tried_new  = False
    data = None

    while time.time() < deadline:
        slice_s = min(4.0, max(1.0, deadline - time.time()))
        data = wait_for_json_codeblock(page, total_timeout_s=slice_s, settle_s=float(json_settle_s))
        if data:
            break

        err = detect_ui_error(page)
        if not err:
            continue
        log(f"[flow] UI error detected: {err}")

        acted = False
        if click_try_again(page):
            log("[flow] clicked 'Try again'")
            acted = True
        elif soft_reload_chat(page, cfg):
            log("[flow] soft reload ok")
            acted = True

        if not tried_same:
            if _resend_same_chat_via_preamble(page, cfg, full_prompt, latch_secs=activation_latch_secs, debug=debug):
                tried_same = True
                continue
            log("[flow] same-chat resend failed")
            tried_same = True

        if not tried_new:
            if _open_new_chat_and_resend(page, cfg, full_prompt, latch_secs=activation_latch_secs, debug=debug):
                tried_new = True
                continue
            log("[flow] new-chat resend failed")
            tried_new = True

        if startover_cb and startover_cb():
            log("[flow] startover ok; resending prompt …")
            return send_prompt_with_websearch(
                page, cfg, full_prompt,
                activation_latch_secs=activation_latch_secs,
                json_total_timeout_s=int(deadline - time.time()) if (deadline - time.time()) > 5 else 10,
                json_settle_s=json_settle_s,
                debug=debug,
                startover_cb=None,
            )

        log("[flow] recovery exhausted")
        break

    dt = int((time.perf_counter() - t0) * 1000)
    log(f"[flow] wait_for_json → {'OK' if data else 'None'} (+{dt} ms)")
    return data
