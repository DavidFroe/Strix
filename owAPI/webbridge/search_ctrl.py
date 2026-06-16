# webbridge/search_ctrl.py
from __future__ import annotations
from contextlib import suppress

from .web_adapter import (
    web_search_is_active,
    enable_web_search as _enable_web_search_impl,
    arm_websearch_protection,
)

_LATCH_KEY = "__wsLastActivatedAt"
_LATCH_SECS_DEFAULT = 8.0  # Zeitfenster: in dieser Zeit NICHT erneut toggeln

def _set_latch(page, secs: float = _LATCH_SECS_DEFAULT) -> None:
    try:
        page.evaluate("(secs, k) => { window[k] = Date.now() + Math.max(0, secs*1000); }",
                      float(secs), _LATCH_KEY)
    except Exception:
        pass

def _is_latched(page) -> bool:
    try:
        return bool(page.evaluate("(k) => (window[k]||0) > Date.now()", _LATCH_KEY))
    except Exception:
        return False

def sticky_verify_only(page, cfg) -> bool:
    """
    Nur Status prüfen & Schutz (Shield) setzen. KEIN Klick/Toggeln.
    """
    active = False
    with suppress(Exception):
        active = bool(web_search_is_active(page))
    with suppress(Exception):
        arm_websearch_protection(page, cfg, ttl_s=900.0)
    return active

def enable_once(page, cfg, *, latch_secs: float = _LATCH_SECS_DEFAULT) -> bool:
    """
    Idempotent:
      - Bereits aktiv → Shield + Latch, True
      - NICHT aktiv → 1 Aktivierungsversuch
      - Latch wird NUR gesetzt, wenn danach wirklich aktiv
    """
    # Schon aktiv?
    try:
        if web_search_is_active(page):
            arm_websearch_protection(page, cfg, ttl_s=900.0)
            _set_latch(page, latch_secs)
            return True
    except Exception:
        pass

    # Genau EIN Versuch
    ok = False
    try:
        ok = bool(_enable_web_search_impl(page, cfg))
    except Exception:
        ok = False

    # Status neu prüfen
    try:
        active = bool(web_search_is_active(page))
    except Exception:
        active = ok

    if active:
        with suppress(Exception):
            arm_websearch_protection(page, cfg, ttl_s=900.0)
        _set_latch(page, latch_secs)
        return True

    # NICHT aktiv → KEIN Latch
    return False

