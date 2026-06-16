# webbridge/web_adapter.py
from __future__ import annotations

import os, re, json, time, subprocess, signal
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from contextlib import suppress

from .config import BridgeConfig
from .click_layer import ClickLayer, ClickMarkup

# ---- optional: OpenCV ----
try:
    import cv2  # type: ignore
    import numpy as np  # type: ignore
except Exception:
    cv2 = None
    np = None



# --- Thread-ID & Sticky-Latch -------------------------------------------------
def _thread_id_from_url(url: str) -> str | None:
    try:
        if "/c/" in url:
            return url.split("/c/")[1].split("?")[0].split("#")[0]
    except Exception:
        pass
    return None

def _ws_mark_sticky(page, *, ttl_s: float = 3600.0) -> None:
    tid = _thread_id_from_url(getattr(page, "url", "") or "")
    if not tid:
        return
    try:
        page.evaluate("""(tid, ttlMs) => {
          if (!window.__wb_ws) window.__wb_ws = {};
          window.__wb_ws[tid] = Date.now() + ttlMs;
        }""", tid, int(max(0.0, ttl_s) * 1000))
    except Exception:
        pass

def _ws_sticky_ok(page) -> bool:
    try:
        return bool(page.evaluate("""(tid) => {
          try {
            const now = Date.now();
            const db = (window.__wb_ws || {});
            return !!(db[tid] && db[tid] > now);
          } catch(e){ return false; }
        }""", _thread_id_from_url(getattr(page, "url", "") or "")))
    except Exception:
        return False



# --------- logging helpers ---------
def _wb_log(msg: str):
    try:
        print(f"[web] {msg}", flush=True)
    except Exception:
        pass

def log(msg: str) -> None:
    print(f"[bridge] {msg}", flush=True)


# --------- process helpers (startover) ---------
def _terminate_proc_like(proc) -> bool:
    try:
        if hasattr(proc, "terminate"):
            proc.terminate()
        if hasattr(proc, "kill"):
            proc.kill()
        return True
    except Exception:
        return False

def _kill_pid_tree_windows(pid: int) -> bool:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

def _wb__safe_call(obj, names, *a, **kw):
    for n in names:
        fn = getattr(obj, n, None)
        if callable(fn):
            return fn(*a, **kw)
    return None

# -------------------- asset / cv helpers --------------------
def _asset_path(cfg, fname: str) -> Path:
    here = Path(__file__).resolve().parent
    root = here.parent

    # Kandidaten in Prioritäts-Reihenfolge
    candidates: list[Path] = []

    # 1) Explizit via Config
    cfg_dir = getattr(cfg, "assets_dir", None)
    if cfg_dir:
        candidates.append(Path(cfg_dir) / fname)

    # 2) Via ENV
    env_dir = os.getenv("WB_ASSETS_DIR")
    if env_dir:
        candidates.append(Path(env_dir) / fname)

    # 3) Standardordner innerhalb/oberhalb von webbridge
    candidates += [
        here / "assets" / fname,    # webbridge/assets/...
        root / "assets" / fname,    # <repo-root>/assets/...
        here / fname,               # (Legacy-Fallback)
        root / fname,               # (Legacy-Fallback)
    ]

    for p in candidates:
        if p.exists():
            return p

    # Last resort: erster Kandidat oder aktuelles Arbeitsverzeichnis
    return candidates[0] if candidates else Path(fname)


def _cv_capture(page, debug_dir: Path = None, label: str = "shot"):
    if cv2 is None or np is None:
        return None
    png = (debug_dir / f"{int(time.time()*1000)}_{label}.png") if debug_dir else None
    try:
        buf = page.screenshot(path=str(png) if png else None, full_page=False)
        arr = np.frombuffer(buf, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None

def _cv_match_multi(img, templ,
                    scales=(0.75, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.20, 1.30)):
    best = None  # (score, (x,y), (w,h), scale, mode)
    for s in scales:
        try:
            th, tw = templ.shape[:2]
            new_w, new_h = max(8, int(tw*s)), max(8, int(th*s))
            t = cv2.resize(templ, (new_w, new_h),
                           interpolation=cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC)
        except Exception:
            continue

        # Farbe
        try:
            res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
            _, mv, _, ml = cv2.minMaxLoc(res)
            cand = (float(mv), (ml[0], ml[1]), (t.shape[1], t.shape[0]), s, "color")
            if best is None or cand[0] > best[0]:
                best = cand
        except Exception:
            pass

        # Grau
        try:
            ig = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            tg = cv2.cvtColor(t,   cv2.COLOR_BGR2GRAY)
            res = cv2.matchTemplate(ig, tg, cv2.TM_CCOEFF_NORMED)
            _, mv, _, ml = cv2.minMaxLoc(res)
            cand = (float(mv), (ml[0], ml[1]), (tg.shape[1], tg.shape[0]), s, "gray")
            if best is None or cand[0] > best[0]:
                best = cand
        except Exception:
            pass
    return best

def _roi_bottom_band(img, band_ratio: float = 0.40):
    h, w = img.shape[:2]
    band = max(24, int(h * float(band_ratio)))
    y1 = h - band
    return img[y1:h, 0:w], (0, y1)

def _annot(img, rect, out: Path):
    try:
        x1,y1,x2,y2 = rect
        cv2.rectangle(img, (x1,y1), (x2,y2), (0,255,0), 2)
        cv2.imwrite(str(out), img)
    except Exception:
        pass

def _cv_find_icon(page, cfg, fname: str, *, prefer_bottom_band=True,
                  min_score: float | None = None, debug_tag: str = "") -> tuple[int,int,float] | None:
    if cv2 is None or np is None:
        _wb_log("OpenCV nicht verfügbar.")
        return None
    p = _asset_path(cfg, fname)
    if not p.exists():
        _wb_log(f"Asset fehlt: {p}")
        return None

    debug_on = bool(os.getenv("WB_CV_DEBUG") or getattr(cfg, "debug_clicks", False))
    debug_dir = Path(getattr(cfg, "debug_dir", "debug"))
    if debug_on:
        debug_dir.mkdir(parents=True, exist_ok=True)

    img = _cv_capture(page, debug_dir if debug_on else None, label=f"cv_{debug_tag or fname}_base")
    if img is None:
        _wb_log("Screenshot fehlgeschlagen.")
        return None
    templ = cv2.imread(str(p), cv2.IMREAD_COLOR)
    if templ is None:
        _wb_log(f"Template nicht lesbar: {p}")
        return None

    thr = float(min_score if min_score is not None else getattr(cfg, "image_match_threshold", 0.80))

    cand = None; off = (0,0)
    if prefer_bottom_band:
        roi, off = _roi_bottom_band(img, band_ratio=float(getattr(cfg, "cv_bottom_band_ratio", 0.40)))
        cand = _cv_match_multi(roi, templ)

    if cand is None or cand[0] < thr:
        cand2 = _cv_match_multi(img, templ)
        if cand is None or (cand2 and cand2[0] > cand[0]):
            cand, off = cand2, (0,0)

    if not cand:
        _wb_log("Kein Match (None).")
        return None

    score, loc, size, scale, mode = cand
    x = int(loc[0] + size[0]/2 + off[0]); y = int(loc[1] + size[1]/2 + off[1])
    _wb_log(f"CV '{fname}': score={score:.3f} scale={scale:.2f} mode={mode} → ({x},{y})")

    if debug_on:
        shot = _cv_capture(page)
        if shot is not None:
            x1,y1 = int(loc[0]+off[0]), int(loc[1]+off[1])
            x2,y2 = x1+int(size[0]), y1+int(size[1])
            _annot(shot, (x1,y1,x2,y2), debug_dir / f"{int(time.time()*1000)}_{debug_tag or fname}_annot.png")

    return (x, y, float(score)) if score >= thr else None


# -------------------- Chrome / CDP --------------------
def start_chrome_debug(cfg: BridgeConfig) -> None:
    exe = Path(getattr(cfg, "chrome_exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe"))
    port = int(getattr(cfg, "cdp_port", 9333))
    addr = str(getattr(cfg, "cdp_addr", "127.0.0.1"))
    user_dir = Path(getattr(cfg, "chrome_user_data_dir",
                            str(Path(os.getenv("LOCALAPPDATA") or ".") / "ChromeDebugUserData")))
    user_dir.mkdir(parents=True, exist_ok=True)

    args = [
        str(exe),
        f"--remote-debugging-port={port}",
        f"--remote-debugging-address={addr}",
        f"--user-data-dir={str(user_dir)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
    ]
    with suppress(Exception):
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    time.sleep(float(getattr(cfg, "wait_cdp_seconds", 5)))

def connect_chrome(pw, cfg: BridgeConfig):
    port = int(getattr(cfg, "cdp_port", 9333))
    addr = str(getattr(cfg, "cdp_addr", "127.0.0.1"))
    url  = f"http://{addr}:{port}"
    log(f"CDP verbinden: {url}")
    return pw.chromium.connect_over_cdp(url)

def _get_goto_timeout_ms(cfg: BridgeConfig) -> int:
    try:
        return int(getattr(cfg, "goto_timeout_ms", None) or os.getenv("WB_GOTO_TIMEOUT_MS") or 25000)
    except Exception:
        return 25000

def pick_or_open_chat_page(browser, chat_url: str):
    for ctx in browser.contexts:
        for p in ctx.pages:
            u = (p.url or "")
            if ("chat.openai" in u) or ("chatgpt.com" in u):
                return p
    goto_timeout = _get_goto_timeout_ms(BridgeConfig())
    ctx = browser.new_context(no_viewport=True, ignore_https_errors=True) if not browser.contexts else browser.contexts[0]
    page = ctx.new_page()
    attempts = 3
    for i in range(attempts):
        try:
            page.goto(chat_url, wait_until="domcontentloaded", timeout=goto_timeout)
            return page
        except Exception as e1:
            with suppress(Exception):
                log(f"Goto domcontentloaded Versuch {i+1}/{attempts} scheiterte: {e1}")
            try:
                page.goto(chat_url, wait_until="load", timeout=goto_timeout)
                return page
            except Exception as e2:
                with suppress(Exception):
                    log(f"Goto load Versuch {i+1}/{attempts} scheiterte: {e2}")
            try:
                page.goto(chat_url, wait_until="networkidle", timeout=goto_timeout)
                return page
            except Exception as e3:
                with suppress(Exception):
                    log(f"Goto networkidle Versuch {i+1}/{attempts} scheiterte: {e3}")
            time.sleep(min(1.5 * (i + 1), 4.0))
            with suppress(Exception):
                page.reload(wait_until="domcontentloaded", timeout=goto_timeout)
    return page



# -------------------- Composer / Sichtbarkeit --------------------
def on_login_page(page) -> bool:
    u = (page.url or "").lower()
    if re.search(r"login|signin|auth|accounts", u):
        return True
    with suppress(Exception):
        if page.get_by_role("button", name=re.compile("Continue|Anmelden|Sign in", re.I)).count() > 0:
            return True
    return False

def _composer_present(page) -> bool:
    try:
        return page.evaluate("""() => !!(
          document.querySelector('[data-testid^="composer"]')
          || document.querySelector('textarea[placeholder*="Message" i]')
          || document.querySelector('[role="textbox"][contenteditable="true"]')
        )""")
    except Exception:
        return False

# bei einfügen in File web_adapter.py  (am besten unter den Guard-/Protection-Funktionen)
# Funktion ws_begin_hold kann entfernt werden
def ws_begin_hold(page, *, ttl_s: float = 25.0, interval_ms: int = 120) -> bool:
    """
    'Hold'-Phase: bis zum Prompt-Submit wird Websearch überwacht und bei
    versehentlichem Off sofort wieder eingeschaltet (DOM-klick auf den Toggle).
    """
    try:
        return bool(page.evaluate("""(ttlMs, intervalMs) => {
          try { if (window.__wsHold?.disable) window.__wsHold.disable(); } catch(e){}
          const rx = /web\\s?search|websuche|im web|search the web|browsing|browse/i;

          const isOn = (el) => !!(el && (
            el.classList.contains('is-active')
            || el.getAttribute('aria-pressed') === 'true'
            || el.getAttribute('aria-checked') === 'true'
            || ((el.getAttribute('data-state')||'').toLowerCase() === 'on')
          ));

          const findBtn = () => {
            const els = Array.from(document.querySelectorAll('button,[role="switch"],[role="checkbox"]'));
            const found = els.find(el => rx.test(((el.innerText||'') + ' ' + (el.ariaLabel || el.getAttribute('aria-label') || '')).toLowerCase()));
            return found || null;
          };

          const S = window.__wsHold = { enabled:true, until: Date.now() + Math.max(1000, ttlMs) };

          S.check = () => {
            try {
              const btn = findBtn();
              if (btn && !isOn(btn)) { try { btn.click(); } catch(e){} }
              if (Date.now() > S.until) { S.disable(); }
            } catch(e){}
          };

          S.disable = () => {
            if (!S.enabled) return true;
            S.enabled = false;
            try { clearInterval(S.timer); } catch(e){}
            try { S.mo && S.mo.disconnect(); } catch(e){}
            return true;
          };

          try {
            S.mo = new MutationObserver(S.check);
            S.mo.observe(document.body, {subtree:true, childList:true, attributes:true});
          } catch(e){}

          S.timer = setInterval(S.check, Math.max(60, intervalMs));
          S.check();
          return true;
        }""", int(max(1.0, ttl_s)*1000), int(max(30, interval_ms))))
    except Exception:
        return False

# bei einfügen in File web_adapter.py
# Funktion ws_end_hold kann entfernt werden
def ws_end_hold(page) -> bool:
    """Beendet die Hold-Überwachung."""
    try:
        return bool(page.evaluate("""() => {
          try { return window.__wsHold?.disable ? window.__wsHold.disable() : true; }
          catch(e){ return false; }
        }"""))
    except Exception:
        return False


def ensure_composer_or_recover(page, cfg: BridgeConfig, *, new_chat: bool = True) -> bool:
    try:
        dismiss_memory_modal(page, cfg)
    except Exception:
        pass

    if _composer_present(page):
        return True
    with suppress(Exception):
        page.goto(cfg.chat_url, wait_until="domcontentloaded", timeout=_get_goto_timeout_ms(cfg))
        time.sleep(0.3)
    if _composer_present(page):
        return True
    if new_chat:
        with suppress(Exception):
            page.get_by_role("link", name=re.compile("New chat|Neuer Chat", re.I)).first.click()
            time.sleep(0.3)
        try:
            dismiss_memory_modal(page, cfg)
        except Exception:
            pass

    with suppress(Exception):
        page.evaluate("""() => {
          (document.querySelector('[data-testid^="composer"]') || document.body)
            .scrollIntoView({block:'end'});
        }""")
        time.sleep(0.2)
    return _composer_present(page)


# -------------------- Websearch (DOM + Maus + CV) --------------------

# === STICKY LOCK: nur minimale Helfer, kein zusätzliches UI-Gefummel ==========
def _current_thread_id(page) -> str:
    try:
        return page.evaluate("() => location.pathname || ''") or ""
    except Exception:
        return ""

def _get_ws_lock(page) -> Tuple[bool, str]:
    try:
        st = page.evaluate("""() => ({
          lock: !!window.__wb_ws_lock,
          tid : window.__wb_ws_tid || ''
        })""")
        return bool(st.get("lock")), str(st.get("tid") or "")
    except Exception:
        return (False, "")

def _set_ws_lock(page, enable: bool) -> None:
    try:
        page.evaluate("""(on) => {
          window.__wb_ws_lock = !!on;
          window.__wb_ws_tid  = location.pathname || '';
        }""", bool(enable))
    except Exception:
        pass

def clear_websearch_lock(page) -> None:
    _set_ws_lock(page, False)

def mark_websearch_sticky_if_active(page, cfg) -> bool:
    """Falls bereits aktiv, Lock setzen + Schutz scharf stellen (ohne weitere Klicks)."""
    if web_search_is_active(page):
        _set_ws_lock(page, True)
        try:
            arm_websearch_protection(page, cfg, ttl_s=900.0)
        except Exception:
            pass
        _wb_log("Sticky: lock gesetzt (bereits aktiv).")
        return True
    return False
# ==============================================================================

def enable_temporary_chat(page, cfg: BridgeConfig) -> bool:
    with suppress(Exception):
        page.get_by_role("button", name=re.compile("Temporary chat|Temporärer Chat", re.I)).first.click(timeout=800)
        time.sleep(0.25)
        return True
    return True

def _dom_enable_web_search(page) -> bool:
    patt = re.compile(r"Web ?search|Websuche|Im Web suchen|Im Internet|Search the web|Browsing|Browse", re.I)
    with suppress(Exception):
        loc = page.get_by_role("button", name=patt)
        if loc.count() == 0:
            return False
        b = loc.first
        eh = b.element_handle()
        if not eh:
            return False
        # Nur klicken, wenn OFF
        is_on = page.evaluate("""(el) => {
            const on = el.classList.contains('is-active')
                    || el.getAttribute('aria-pressed') === 'true'
                    || el.getAttribute('aria-checked') === 'true'
                    || ((el.getAttribute('data-state')||'').toLowerCase() === 'on');
            return !!on;
        }""", eh)
        if is_on:
            return True
        b.click()
        time.sleep(0.25)
        return True
    return False

# ersetzen in File web_adapter.py
# Funktion web_search_is_active kann entfernt werden
# ersetzen in File web_adapter.py
# Funktion web_search_is_active kann entfernt werden
def web_search_is_active(page, cfg=None) -> bool:
    # 1) Sticky
    if _ws_sticky_ok(page):
        return True

    # 2) DOM
    try:
        dom_on = page.evaluate("""() => {
            const rx = /web\\s?search|websuche|im web|browsing|browse/i;
            const toggles = [...document.querySelectorAll('button,[role="switch"],[role="checkbox"]')];
            for (const el of toggles) {
              const txt = ((el.innerText||'') + ' ' + (el.ariaLabel||el.getAttribute('aria-label')||'')).toLowerCase();
              if (!rx.test(txt)) continue;
              const on = el.classList.contains('is-active')
                      || el.getAttribute('aria-pressed') === 'true'
                      || el.getAttribute('aria-checked') === 'true'
                      || ((el.getAttribute('data-state')||'').toLowerCase() === 'on');
              if (on) return true;
            }
            const chips = [...document.querySelectorAll('[data-testid*="chip"],[class*="chip"],[class*="pill"],[class*="tag"]')];
            for (const c of chips) {
              const t = (c.innerText||'').toLowerCase();
              if (/\\bweb\\b/.test(t)) {
                if (c.classList.contains('is-active')
                    || c.getAttribute('aria-selected')=='true'
                    || c.getAttribute('aria-pressed')=='true'
                    || ((c.getAttribute('data-state')||'').toLowerCase() == 'on')
                    || c.matches('[aria-current="true"]')) return true;
              }
            }
            return false;
        }""")
        if dom_on:
            return True
    except Exception:
        pass

    # 3) CV-Heuristik: Lupe-Icon gefunden → aktiv
    try:
        thr = float(getattr(cfg, "cv_thr_search_active", 0.95)) if cfg else 0.95
        hit = _cv_find_icon(page, cfg, "search.jpg", debug_tag="search",
                            prefer_bottom_band=True, min_score=thr)
        if hit:
            sx, sy, _ = hit
            _wb_add_no_click_zone(int(sx), int(sy), radius=24, ttl_s=900.0, tag="search-toggle")
            _ws_mark_sticky(page, ttl_s=float(getattr(cfg, "cv_search_sticky_ttl", 20.0)) if cfg else 20.0)
            _wb_log("WS active (via CV search.jpg).")
            return True
    except Exception:
        pass

    return False


    # 3) CV-Precheck (heuristisch): Lupe am unteren Rand gefunden ⇒ sehr wahrscheinlich ON
    try:
        thr = float(getattr(cfg, "cv_thr_search_on", 0.92)) if cfg else 0.92
        hit = _cv_find_icon(page, cfg, "search.jpg", debug_tag="search",
                            prefer_bottom_band=True, min_score=thr)
        if hit:
            sx, sy, _ = hit
            _wb_add_no_click_zone(int(sx), int(sy), radius=22, ttl_s=900.0, tag="search-toggle")
            # kurz „sticky“ setzen, damit wir nicht sofort wieder prüfen/aktivieren
            _ws_mark_sticky(page, ttl_s=float(getattr(cfg, "cv_precheck_sticky_ttl", 25.0)) if cfg else 25.0)
            _wb_log("WS active (CV-precheck).")
            return True
    except Exception:
        pass

    return False



# ersetzen in File web_adapter.py
# Funktion shield_websearch_toggle kann entfernt werden
def shield_websearch_toggle(page, enable: bool, *, ttl_s: float = 900.0, margin: int = 16) -> bool:
    """
    Blockiert Klick/Key-Toggle auf Websearch-UI (Toggle/Chips) per Capture-Listener.
    """
    try:
        if enable:
            return bool(page.evaluate("""(ttlMs, m) => {
              try {
                const rx = /web\\s?search|websuche|im web|search the web|web-recherche|im internet/i;

                const rectOf = (el, pad=m) => {
                  if (!el) return null;
                  const r = el.getBoundingClientRect();
                  return { x1: r.left - pad, y1: r.top - pad, x2: r.right + pad, y2: r.bottom + pad };
                };

                const pickCandidates = () => {
                  const bag = new Set();

                  // 1) echte Toggles (Button / Switch)
                  document.querySelectorAll('button,[role="switch"],[role="checkbox"]').forEach(el => {
                    const txt = ((el.innerText || '') + ' ' + (el.ariaLabel || el.getAttribute('aria-label') || '')).toLowerCase();
                    if (rx.test(txt)) bag.add(el);
                  });

                  // 2) Chips/Pills in der Toolbar/Composer-Leiste
                  document.querySelectorAll('[data-testid*="chip"],[class*="chip"],[class*="pill"],[class*="tag"]').forEach(el => {
                    const txt = (el.innerText || '').toLowerCase();
                    if (/\\bweb\\b/.test(txt)) bag.add(el);
                  });

                  // 3) Icon-only Buttons (SVG mit aria-label)
                  document.querySelectorAll('button svg[aria-label],svg[aria-label]').forEach(svg => {
                    const lab = (svg.getAttribute('aria-label') || '').toLowerCase();
                    if (rx.test(lab)) bag.add(svg.closest('button') || svg);
                  });

                  return Array.from(bag).map(el => rectOf(el)).filter(Boolean);
                };

                const inside = (rects, x, y) => rects.some(r => x >= r.x1 && x <= r.x2 && y >= r.y1 && y <= r.y2);

                // State
                if (!window.__wsToggleShield) window.__wsToggleShield = {};
                const S = window.__wsToggleShield;
                S.enabled = true;
                S.expires = Date.now() + Math.max(1000, ttlMs);
                S.rects = pickCandidates();

                const recalc = () => {
                  if (!S.enabled) return;
                  if (Date.now() > S.expires) { disable(); return; }
                  S.rects = pickCandidates();
                };

                const guard = (ev) => {
                  try {
                    if (!S.enabled) return;
                    if (!S.rects || !S.rects.length) return;
                    const x = ev.clientX ?? (ev.touches && ev.touches[0] && ev.touches[0].clientX) ?? -1;
                    const y = ev.clientY ?? (ev.touches && ev.touches[0] && ev.touches[0].clientY) ?? -1;

                    // Mouse/Pointer hit-test
                    const byPoint = (x >= 0 && y >= 0) && inside(S.rects, x, y);

                    // Keyboard toggle on focused element inside a protected rect
                    let byKey = false;
                    if (ev.type === 'keydown') {
                      const k = (ev.key || '').toLowerCase();
                      if (k === ' ' || k === 'enter') {
                        const a = document.activeElement;
                        if (a) {
                          const r = a.getBoundingClientRect();
                          byKey = inside(S.rects, (r.left + r.right) / 2, (r.top + r.bottom) / 2);
                        }
                      }
                    }

                    if (byPoint || byKey) {
                      ev.stopImmediatePropagation();
                      ev.preventDefault();
                      return false;
                    }
                  } catch (e) {}
                };

                const disable = () => {
                  if (!S.enabled) return true;
                  S.enabled = false;
                  for (const type of ['pointerdown','mousedown','click','keydown','touchstart']) {
                    document.removeEventListener(type, guard, true);
                  }
                  if (S.timer) { clearInterval(S.timer); S.timer = null; }
                  if (S.mo) { try { S.mo.disconnect(); } catch(e){} S.mo = null; }
                  return true;
                };

                // Attach listeners (capture)
                for (const type of ['pointerdown','mousedown','click','keydown','touchstart']) {
                  document.addEventListener(type, guard, true);
                }

                // MutationObserver + Polling zur Stabilität bei UI-Reflow
                try {
                  S.mo = new MutationObserver(() => recalc());
                  S.mo.observe(document.body, { subtree: true, childList: true, attributes: true });
                } catch(e) {}
                S.timer = setInterval(recalc, 800);

                // Auto-Disable, wenn TTL erreicht
                if (S.kill) clearTimeout(S.kill);
                S.kill = setTimeout(disable, ttlMs);

                S.guard = guard;
                S.disable = disable;
                return true;
              } catch(e) { return false; }
            }""", int(max(0.0, ttl_s) * 1000), int(max(0, margin))))
        else:
            return bool(page.evaluate("""() => {
              try {
                if (!window.__wsToggleShield?.enabled) return true;
                return window.__wsToggleShield.disable ? window.__wsToggleShield.disable() : true;
              } catch(e){ return false; }
            }"""))
    except Exception:
        return False



# ersetzen in File web_adapter.py
# Funktion arm_websearch_protection kann entfernt werden
def arm_websearch_protection(page, cfg, *, ttl_s: float = 900.0) -> None:
    """
    Kombinierter Schutz:
      (1) DOM-basierter Click-Blocker (Toggles + Chips/Pills)
      (2) CV-Overlay (runder No-Click-Bereich um das Lupe-Icon)
    """
    try:
        margin = int(getattr(cfg, "ws_shield_margin", 18))
    except Exception:
        margin = 18
    try:
        shield_websearch_toggle(page, True, ttl_s=ttl_s, margin=margin)
    except Exception:
        pass
    # Bildbasierter Zusatz-Ring um das Such-Icon
    try:
        hit = _cv_find_icon(page, cfg, "search.jpg", debug_tag="search",
                            min_score=float(getattr(cfg, "cv_thr_search", 0.80)))
        if hit:
            sx, sy, _ = hit
            _wb_add_no_click_zone(int(sx), int(sy), radius=28, ttl_s=ttl_s, tag="search-toggle")
    except Exception:
        pass


def disarm_websearch_protection(page) -> None:
    try:
        shield_websearch_toggle(page, False)
    except Exception:
        pass

# --- no-click zone (klein) -----------------------------------
def _wb__noclick_state():
    if not hasattr(_wb__noclick_state, "zones"):
        _wb__noclick_state.zones = []  # [(x1,y1,x2,y2,expires,tag)]
    return _wb__noclick_state.zones

def _wb_add_no_click_zone(cx: int, cy: int, *, radius: int = 22, ttl_s: float = 300.0, tag: str = "search-toggle"):
    zones = _wb__noclick_state()
    now = time.time()
    zones.append((cx - radius, cy - radius, cx + radius, cy + radius, now + ttl_s, tag))
    try:
        _wb_log(f"Guard: no-click zone gesetzt um ({cx},{cy}) r={radius} tag={tag} für {int(ttl_s)}s.")
    except Exception:
        pass

def _wb_point_blocked(x: int, y: int) -> bool:
    zones = _wb__noclick_state()
    now = time.time()
    keep = []
    blocked = False
    for (x1, y1, x2, y2, exp, tag) in zones:
        if now <= exp:
            keep.append((x1, y1, x2, y2, exp, tag))
            if x1 <= x <= x2 and y1 <= y <= y2:
                blocked = True
    _wb__noclick_state.zones = keep  # type: ignore[attr-defined]
    return blocked

def _wb_safe_click(page, x: int, y: int, *, label: str = "") -> bool:
    if _wb_point_blocked(x, y):
        try:
            _wb_log(f"Guard: Click in geschützter Zone unterdrückt ({label}) @({x},{y}).")
        except Exception:
            pass
        return False
    try:
        page.mouse.move(x, y, steps=4)
        page.mouse.click(x, y, click_count=1)
        return True
    except Exception:
        return False

# ersetzen in File web_adapter.py
# Funktion enable_web_search kann entfernt werden
def enable_web_search(page, cfg) -> bool:
    """DOM-first Aktivierung; CV-Fallback; nur nach verifiziertem ON wird „sticky“ gesetzt."""
    # Sticky → nur Schutz scharfschalten
    if _ws_sticky_ok(page):
        try: arm_websearch_protection(page, cfg, ttl_s=900.0)
        except Exception: pass
        log("Websearch sticky-latched (skip).")
        return True

    # Bereits aktiv?
    if web_search_is_active(page):
        log("Websearch bereits aktiv.")
        try:
            arm_websearch_protection(page, cfg, ttl_s=900.0)
            _ws_mark_sticky(page, ttl_s=900.0)
        except Exception:
            pass
        return True

    # DOM-Toggle
    if _dom_enable_web_search(page):
        time.sleep(0.18)
        if web_search_is_active(page):
            log("Websearch via DOM aktiviert.")
            try:
                arm_websearch_protection(page, cfg, ttl_s=900.0)
                _ws_mark_sticky(page, ttl_s=900.0)
            except Exception:
                pass
            return True

    # [+] → DOM-Toggle nochmal versuchen
    with suppress(Exception):
        page.get_by_role("button", name=re.compile(r"^\\+$")).first.click(timeout=600)
        time.sleep(0.22)
    if _dom_enable_web_search(page):
        time.sleep(0.18)
        if web_search_is_active(page):
            log("Websearch via DOM (+ → Button) aktiviert.")
            try:
                arm_websearch_protection(page, cfg, ttl_s=900.0)
                _ws_mark_sticky(page, ttl_s=900.0)
            except Exception:
                pass
            return True

    # CV-Fallback
    if enable_web_search_via_cv(page, cfg):
        if web_search_is_active(page):
            log("Websearch via CV aktiviert.")
            try:
                arm_websearch_protection(page, cfg, ttl_s=900.0)
                _ws_mark_sticky(page, ttl_s=900.0)
            except Exception:
                pass
            return True

    # Finaler Post-Check
    if web_search_is_active(page):
        log("Websearch aktiv (post-check).")
        try:
            arm_websearch_protection(page, cfg, ttl_s=900.0)
            _ws_mark_sticky(page, ttl_s=900.0)
        except Exception:
            pass
        return True

    return False




def mouse_fallback_enable_web_search(
    page,
    cfg: BridgeConfig,
    retries: int = 1,
    *,
    mode: str,
    abs_coords: Dict[str, Tuple[float, float]],
    rel_coords: Dict[str, Tuple[float, float]],
) -> bool:
    # === STICKY: wenn Lock für aktuellen Thread → gar nichts tun
    cur_tid = _current_thread_id(page)
    locked, lock_tid = _get_ws_lock(page)
    if locked and lock_tid == cur_tid:
        _wb_log("Sticky: Fallback übersprungen (gelocked).")
        return True

    if web_search_is_active(page):
        log("Websearch bereits aktiv (vor Fallback).")
        _set_ws_lock(page, True)
        return True
    cl = ClickLayer(page, cfg)
    plus_img = _asset_path(cfg, "plus.jpg")
    web_img  = _asset_path(cfg, "websearch.jpg")
    plus_box = getattr(cfg, "plus_box_css", None)
    web_box  = getattr(cfg, "web_box_css",  None)

    # tolerantere Default-Schwellen; via cfg überschreibbar
    thr_plus = float(getattr(cfg, "cv_thr_plus", getattr(cfg, "image_match_threshold", 0.74)))
    thr_web  = float(getattr(cfg, "cv_thr_web",  getattr(cfg, "image_match_threshold", 0.74)))

    markup = [
        ClickMarkup(name="plus", role="button", text=r"^\+$",
                    selectors=["[data-testid='composer-plus-btn']"],
                    img_path=str(plus_img) if plus_img.exists() else None,
                    expected_box_css=plus_box, min_score=thr_plus),
        ClickMarkup(name="more", role="button", text=r"More|Mehr",
                    selectors=["[data-testid='composer-more-btn']"]),
        ClickMarkup(name="web",  role="button",
                    text=r"Web ?search|Websuche|Im Web suchen|Search the web",
                    img_path=str(web_img) if web_img.exists() else None,
                    expected_box_css=web_box, min_score=thr_web),
    ]

    with suppress(Exception):
        page.evaluate("""() => {
            const el = document.querySelector('[data-testid^="composer"]') || document.body;
            el.scrollIntoView({block:'end'});
        }""")
        time.sleep(0.1)

    if _dom_enable_web_search(page):
        return True

    for _ in range(max(1, retries) + 1):
        ok = cl.hover_menu_then_click(
            mode=mode,
            abs_coords=abs_coords if mode in ("px", "screen") else {},
            rel_coords=rel_coords if mode == "rel" else {},
            markup=markup,
            hover_backend=getattr(cfg, "hover_backend", None),
            dwell_more=float(getattr(cfg, "hover_dwell_more", 0.35))
        )
        if ok:
            return True

        if getattr(cfg, "allow_window_maximize", True):
            cl.try_maximize_window(getattr(cfg, "window_title_hint", "Chrome"))
        time.sleep(0.3)
    if enable_web_search_via_cv(page, cfg):
        if web_search_is_active(page):
            log("Websearch via CV aktiviert.")
            _set_ws_lock(page, True)
            return True
    if web_search_is_active(page):
        log("Websearch aktiv (post-check nach Fallback).")
        _set_ws_lock(page, True)
        return True
    return False


def startover_browser(adapter) -> bool:
    """
    Schließt Browser/Context/Page + Playwright hart und startet neu (inkl. CDP-Reconnect).
    Erwartete optionale Felder am Adapter:
      - page, context, browser
      - _play (sync_playwright Context Manager) ODER playwright (Objekt mit .stop())
      - chrome_proc (subprocess Popen) ODER chrome_pid (int)
      - cdp_port (int) / cdp_url (str)
      - Boot-Sequenzen: boot / startup / init / open / connect / start / _boot / _init_browser / ensure_cdp / connect_cdp
      - post hooks: after_startup / post_boot / prepare_page / prepare_chat / prepare_chat_page
    Rückgabe: True bei erfolgreichem Neustart + Minimalprüfung.
    """
    ok = True
    _wb_log("Startover: beginne harten Neustart …")

    # 1) Seiten & Kontexte schließen
    for name in ("page", "context", "browser"):
        obj = getattr(adapter, name, None)
        if obj is not None:
            try:
                obj.close()
                _wb_log(f"Startover: {name}.close() ok.")
            except Exception as e:
                _wb_log(f"Startover: {name}.close() fehlschlag: {e!r}")
                ok = False
            try:
                setattr(adapter, name, None)
            except Exception:
                pass

    # 2) Playwright stoppen
    for pw_name in ("playwright", "_play"):
        pw = getattr(adapter, pw_name, None)
        if pw is not None:
            try:
                stop = getattr(pw, "stop", None)
                if callable(stop):
                    stop()
                    _wb_log("Startover: Playwright gestoppt.")
            except Exception as e:
                _wb_log(f"Startover: Playwright stop fehlschlag: {e!r}")
                ok = False
            try:
                setattr(adapter, pw_name, None)
            except Exception:
                pass

    # 3) Chrome-Prozess hart beenden, falls wir ihn gestartet haben
    killed = False
    proc = getattr(adapter, "chrome_proc", None)
    if proc is not None:
        killed = _terminate_proc_like(proc)
        try: setattr(adapter, "chrome_proc", None)
        except Exception: pass

    pid = getattr(adapter, "chrome_pid", None)
    if isinstance(pid, int) and pid > 0:
        try:
            if os.name == "nt":
                killed = _kill_pid_tree_windows(pid) or killed
            else:
                os.kill(pid, signal.SIGTERM); time.sleep(0.3)
                with open(os.devnull, "wb"):
                    try: os.kill(pid, signal.SIGKILL)
                    except Exception: pass
                killed = True
            _wb_log(f"Startover: Chrome PID {pid} beendet.")
        except Exception as e:
            _wb_log(f"Startover: Chrome PID kill fehlschlag: {e!r}")
            ok = False
        try: setattr(adapter, "chrome_pid", None)
        except Exception: pass

    # 4) kurze Pause, damit der Port frei wird
    time.sleep(0.4)

    # 5) Neu-Boot
    boot_names = ("boot", "startup", "init", "open", "connect", "start", "_boot", "_init_browser", "ensure_cdp", "connect_cdp")
    _wb_log("Startover: starte Playwright/Browser neu …")
    try:
        ret = _wb__safe_call(adapter, boot_names)
        if ret is False: ok = False
        _wb__safe_call(adapter, ("after_startup", "post_boot", "prepare_page", "prepare_chat", "prepare_chat_page"))
    except Exception as e:
        _wb_log(f"Startover: Neustart fehlschlagen: {e!r}")
        return False

    # 6) Minimal-Validation
    page_ok = getattr(adapter, "page", None) is not None
    ctx_ok  = getattr(adapter, "context", None) is not None
    br_ok   = getattr(adapter, "browser", None) is not None
    if not (page_ok and ctx_ok and br_ok):
        _wb_log(f"Startover: Validierung fehlgeschlagen (page={page_ok}, ctx={ctx_ok}, br={br_ok}).")
        return False

    _wb_log("Startover: Browser erfolgreich neu gestartet und verbunden.")
    return ok


# -------------------- CV-Overlay (optional) --------------------
def _show_cv_overlay(page, *, title: str, detail: str, img_path: str | None = None, timeout_s: float = 10.0) -> str:
    """Zeigt oben links ein kleines TL-Overlay mit Titel/Detail + Button 'Programm beenden'.
    Läuft nach timeout_s Sekunden automatisch weiter.
    Rückgabe: 'abort' (Button gedrückt) oder 'timeout' (automatisch weiter).
    """
    import base64, os
    img_b64 = None
    try:
        if img_path and os.path.exists(img_path):
            with open(img_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception:
        img_b64 = None

    js = """
    (t, d, imgData, timeoutMs) => {
      try { const old = document.getElementById('wb-cv-overlay'); if (old) old.remove(); } catch(e){}
      const wrap = document.createElement('div');
      wrap.id = 'wb-cv-overlay';
      Object.assign(wrap.style, {
        position:'fixed', left:'8px', top:'8px',
        maxWidth:'360px', padding:'10px 12px',
        background:'rgba(0,0,0,0.85)', color:'#fff',
        font:'12px/1.35 -apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif',
        border:'1px solid rgba(255,255,255,0.25)', borderRadius:'10px',
        zIndex: 2147483647, boxShadow:'0 4px 16px rgba(0,0,0,0.4)'
      });
      const h = document.createElement('div'); h.style.fontWeight='600'; h.style.marginBottom='6px'; h.textContent=t;
      const p = document.createElement('div'); p.style.whiteSpace='pre-wrap'; p.textContent=d;
      const cnt = document.createElement('div'); cnt.id='wb-cv-count'; cnt.style.marginTop='6px';
      const btn = document.createElement('button');
      btn.textContent='Programm beenden';
      Object.assign(btn.style, {
        marginTop:'8px', padding:'6px 10px', cursor:'pointer',
        borderRadius:'8px', border:'1px solid #444', background:'#d33', color:'#fff'
      });
      btn.onclick = () => { window.__wb_overlay_decision = 'abort'; try { wrap.remove(); } catch(_){} };
      wrap.appendChild(h);
      if (imgData) {
        const img = document.createElement('img');
        img.src = 'data:image/png;base64,'+imgData;
        img.alt = 'CV-Pattern';
        img.style.maxWidth='100%'; img.style.display='block'; img.style.margin='6px 0';
        wrap.appendChild(img);
      }
      wrap.appendChild(p);
      wrap.appendChild(cnt);
      wrap.appendChild(btn);
      document.body.appendChild(wrap);
      window.__wb_overlay_decision = '';
      const end = Date.now() + timeoutMs;
      const tick = () => {
        const rest = Math.max(0, end - Date.now());
        const s = Math.ceil(rest/1000);
        cnt.textContent = 'Automatisch weiter in ' + s + ' s …';
        if (rest <= 0) {
          window.__wb_overlay_decision = 'timeout';
          try { wrap.remove(); } catch(_) {}
          return;
        }
        window.requestAnimationFrame(tick);
      };
      tick();
      return true;
    }
    """
    try:
        page.evaluate(js, title, detail, img_b64, int(max(0, timeout_s) * 1000))
    except Exception:
        return "timeout"

    t0 = time.perf_counter()
    while (time.perf_counter() - t0) < (timeout_s + 0.25):
        try:
            dec = page.evaluate("() => (window.__wb_overlay_decision || '')")
            if dec == "abort":
                return "abort"
            if dec == "timeout":
                return "timeout"
        except Exception:
            break
        time.sleep(0.10)
    try:
        page.evaluate("() => { const el = document.getElementById('wb-cv-overlay'); if (el) el.remove(); }")
    except Exception:
        pass
    return "timeout"

# --- UI-Error detection & recovery ------------------------------------------------
def detect_ui_error(page) -> str | None:
    try:
        return page.evaluate("""() => {
          const txt = (el) => (el?.innerText || el?.textContent || '').toLowerCase();
          const markers = [
            'something seems to have gone wrong',
            'something went wrong',
            'hm…something seems to have gone wrong',
            'hm...something seems to have gone wrong',
            'network error',
            'this content is unavailable'
          ];
          // 1) Alerts/Toasts
          const nodes = Array.from(document.querySelectorAll('[role="alert"], [data-testid*="toast"], [class*="toast"], [class*="error"]'));
          for (const n of nodes) {
            const t = txt(n);
            if (!t) continue;
            for (const m of markers) if (t.includes(m)) return t;
          }
          // 2) Fallback: Volltextscan
          const body = txt(document.body);
          for (const m of markers) if (body.includes(m)) return m;
          return null;
        }""")
    except Exception:
        return None


# bei einfügen in File web_adapter.py
# Funktion _mask_web_suggestions kann entfernt werden
def _mask_web_suggestions(page, ttl_ms: int = 1800) -> bool:
    """Deaktiviert Pointer-Events auf den Websearch-Vorschlägen für kurze Zeit (Tilly-Norwood-Fix)."""
    try:
        return bool(page.evaluate("""(ttlMs) => {
          try { const old = document.getElementById('wb-ws-mask'); if (old) old.remove(); } catch(e){}
          const css = `
            /* Links/Items unterhalb der Websuche vorübergehend nicht klickbar */
            [data-testid*="web"] a, [data-testid*="websearch"] a,
            [role="list"] a, [role="listitem"] a {
              pointer-events: none !important;
            }`;
          const s = document.createElement('style');
          s.id = 'wb-ws-mask'; s.textContent = css;
          document.head.appendChild(s);
          setTimeout(() => { try { s.remove(); } catch(e){} }, Math.max(300, ttlMs|0));
          return true;
        }""", int(max(300, ttl_ms))))
    except Exception:
        return False


def click_try_again(page) -> bool:
    from contextlib import suppress
    sels = [
        "button:has-text('Try again')", "button:has-text('Retry')",
        "button:has-text('Erneut')", "button:has-text('Nochmal')",
        "text=Try again"
    ]
    for sel in sels:
        with suppress(Exception):
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_enabled():
                loc.first.click()
                page.wait_for_timeout(300)
                return True
    return False

def soft_reload_chat(page, cfg) -> bool:
    try:
        page.reload(wait_until="domcontentloaded", timeout=_get_goto_timeout_ms(cfg))
    except Exception:
        return False
    with suppress(Exception):
        page.evaluate("""() => {
          (document.querySelector('[data-testid^="composer"]') || document.body)
            .scrollIntoView({block:'end'});
        }""")
        page.wait_for_timeout(150)
    return bool(_composer_present(page))

# ersetzen in File web_adapter.py
# Funktion enable_web_search_via_cv kann entfernt werden
def enable_web_search_via_cv(page, cfg) -> bool:
    """
    CV-only Aktivierung:
      1) [+] per CV klicken
      2) [⋯ More] hovern
      3) Menüpunkt [Web search] klicken
      4) Aktivierung checken (DOM-only)
      5) KEINE versehentlichen Klicks auf Vorschläge (Maskierung)
    """
    try:
        import cv2  # noqa: F401
        import numpy as np  # noqa: F401
    except Exception:
        _wb_log("CV-Fallback: OpenCV fehlt.")
        return False

    from contextlib import suppress
    with suppress(Exception):
        page.evaluate("""() => {
            const el = document.querySelector('[data-testid^="composer"]') || document.body;
            el.scrollIntoView({block:'end'});
        }""")
        time.sleep(0.10)

    def _poll_active(total_ms=1200, step_ms=80) -> bool:
        t0 = time.perf_counter()
        while (time.perf_counter() - t0) * 1000 < total_ms:
            try:
                if web_search_is_active(page):
                    return True
            except Exception:
                pass
            time.sleep(max(0.02, step_ms / 1000.0))
        return False

    def _defuse_post_click():
        with suppress(Exception): page.keyboard.press("Escape")
        with suppress(Exception): page.mouse.move(10, 10, steps=6)

    # 1) PLUS
    plus = _cv_find_icon(page, cfg, "plus.jpg", debug_tag="plus",
                         min_score=float(getattr(cfg, "cv_thr_plus", 0.78)))
    if not plus:
        _wb_log("CV: [+]-Icon nicht gefunden → abbrechen.")
        return False
    px, py, _ = plus
    _wb_safe_click(page, px, py, label="plus")
    time.sleep(float(getattr(cfg, "mouse_after_plus_sleep", 0.18)))

    # 2) MORE (hover)
    more = _cv_find_icon(page, cfg, "more.jpg", debug_tag="more",
                         min_score=float(getattr(cfg, "cv_thr_more", 0.76)))
    if not more:
        _wb_log("CV: [⋯ More]-Icon nicht gefunden → abbrechen.")
        return False
    mx, my, _ = more
    with suppress(Exception):
        steps = max(1, int(getattr(cfg, "mouse_move_duration", 0.10) * 60))
        page.mouse.move(mx - 140, my, steps=steps)
        page.mouse.move(mx, my, steps=steps)
        for _ in range(2):
            page.mouse.move(mx + 6, my, steps=2)
            page.mouse.move(mx - 6, my, steps=2)
            page.mouse.move(mx, my, steps=2)
    time.sleep(float(getattr(cfg, "hover_dwell_more", 0.30)))

    # 3) WEB SEARCH (Menüeintrag)
    web = _cv_find_icon(page, cfg, "websearch.jpg", debug_tag="web",
                        min_score=float(getattr(cfg, "cv_thr_web", 0.74)))
    if not web:
        _wb_log("CV: Menüpunkt [Web search] nicht gefunden → abbrechen.")
        return False
    wx, wy, _ = web
    _wb_safe_click(page, wx, wy, label="websearch-menu")
    time.sleep(float(getattr(cfg, "mouse_after_click_sleep", 0.20)))

    # <<< NEU: Vorschlags-Klicks 1–2 s blocken (Tilly-Norwood-Fix) >>>
    _mask_web_suggestions(page, int(getattr(cfg, "ws_mask_ttl_ms", 2000)))

    # 4) Aktivierung prüfen (DOM-only)
    if _poll_active(1600, 80):
        _wb_log("CV: Websuche aktiviert.")
        try:
            arm_websearch_protection(page, cfg, ttl_s=900.0)
        except Exception:
            pass
        _defuse_post_click()
        return True

    # 5) Offset-Retry NUR solange der Menüpunkt noch sichtbar ist
    for dx in (2, -2):
        still_menu = _cv_find_icon(
            page, cfg, "websearch.jpg", debug_tag="web",
            min_score=float(getattr(cfg, "cv_thr_web", 0.74))
        )
        if not still_menu:
            break  # Menü ist weg → keine „Blindklicks“ (würden Vorschläge treffen)
        _mask_web_suggestions(page, int(getattr(cfg, "ws_mask_ttl_ms", 1800)))
        _wb_safe_click(page, wx + dx, wy, label=f"websearch-menu-offset-{dx}")
        time.sleep(float(getattr(cfg, "mouse_after_click_sleep", 0.20)))
        if _poll_active(900, 70):
            _wb_log(f"CV: Websuche aktiviert (offset {dx},0).")
            try:
                arm_websearch_protection(page, cfg, ttl_s=900.0)
            except Exception:
                pass
            _defuse_post_click()
            return True

    _wb_log("CV: Websuche ließ sich nicht zuverlässig aktivieren (nach Retry).")
    return False



# -------------------- Guard-Fallback: CV-Spot sofort schützen ----------------
def guard_websearch_toggle_now(page, cfg, *, ttl_s: float = 600.0, radius: int = 22) -> None:
    try:
        hit = _cv_find_icon(page, cfg, "search.jpg", debug_tag="search",
                            min_score=float(getattr(cfg, "cv_thr_search", 0.80)))
        if hit:
            sx, sy, _ = hit
            _wb_add_no_click_zone(int(sx), int(sy), radius=radius, ttl_s=ttl_s, tag="search-toggle")
            _wb_log("Guard: Websuche-Toggle Schutzzone manuell gesetzt.")
    except Exception:
        pass


# -------------------- Websearch-Searchbox helpers --------------------
def focus_web_search_box(page) -> bool:
    try:
        return page.evaluate("""() => {
          const q = (sel) => document.querySelector(sel);

          const cands = [
            "input[placeholder*='Search the web' i]",
            "input[aria-label*='Search the web' i]",
            "[data-testid*='web'] input[placeholder i]",
            "[data-testid*='websearch'] input",
            "[data-testid*='search'] input",
            "[data-testid*='web'] [role='textbox'][contenteditable='true']",
            "[role='search'] [role='textbox'], [role='searchbox']",
          ];

          let el = null;
          for (const sel of cands) {
            el = q(sel);
            if (el) break;
          }

          if (!el) {
            const all = [...document.querySelectorAll('[placeholder],[aria-label],[role=\"textbox\"]')];
            el = all.find(n => /search the web|im web|websuche/i.test(
              (n.getAttribute('placeholder')||n.getAttribute('aria-label')||n.textContent||'')+''
            ));
          }

          if (!el) return false;

          if (el.isContentEditable) {
            el.innerHTML = '';
          } else if ('value' in el) {
            el.value = '';
            try { el.dispatchEvent(new Event('input', {bubbles:true})); } catch(e) {}
          }
          el.focus();
          try {
            if (el.isContentEditable) {
              const sel = window.getSelection();
              const r = document.createRange(); r.selectNodeContents(el); r.collapse(false);
              sel.removeAllRanges(); sel.addRange(r);
            }
          } catch(e) {}
          return document.activeElement === el;
        }""")
    except Exception:
        return False

def type_into_web_search_box(page, text: str, *, auto_submit: bool = False) -> bool:
    ok = False
    try:
        page.keyboard.insert_text(text)
        ok = True
    except Exception:
        pass

    if not ok:
        try:
            page.evaluate("""(t) => {
              const el = document.activeElement;
              if (!el) throw new Error('no active element');
              if (el.isContentEditable) { el.innerText = t; }
              else if ('value' in el) { el.value = t; el.dispatchEvent(new Event('input', {bubbles:true})); }
              else { throw new Error('active element not writable'); }
            }""", text)
            ok = True
        except Exception:
            pass

    if not ok:
        try:
            page.keyboard.type(text, delay=0)
            ok = True
        except Exception:
            pass

    if auto_submit:
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass
    return ok


# --- Memory modal: dismiss ------------------------------------------------------
def _click_button_by_names(page, names_regex_list) -> bool:
    from contextlib import suppress
    for rx in names_regex_list:
        with suppress(Exception):
            loc = page.get_by_role("button", name=rx)
            if loc.count() > 0 and loc.first.is_enabled():
                loc.first.click()
                page.wait_for_timeout(200)
                return True
    return False

def dismiss_memory_modal(page, cfg) -> bool:
    """
    Schließt das 'ChatGPT now has memory' Modal, wenn sichtbar.
    1) DOM-first: 'Not now' / 'Jetzt nicht' / 'Nicht jetzt'
    2) CV-Fallback: notnow.jpg (über _cv_find_icon)
    3) ESC-Fallback
    """
    # 1) DOM-first
    try:
        if page.get_by_role("dialog").count() > 0:
            rx = [
                re.compile(r"Not\s?now", re.I),
                re.compile(r"Jetzt\s+nicht", re.I),
                re.compile(r"Nicht\s+jetzt", re.I),
            ]
            if _click_button_by_names(page, rx):
                _wb_log("Memory modal: dismissed via DOM.")
                return True
    except Exception:
        pass

    # 2) CV-Fallback
    try:
        hit = _cv_find_icon(page, cfg, "notnow.jpg", prefer_bottom_band=False,
                            min_score=float(getattr(cfg, "cv_thr_notnow", 0.82)),
                            debug_tag="notnow")
        if hit:
            x, y, score = hit
            _wb_safe_click(page, x, y, label="memory-notnow")
            page.wait_for_timeout(180)
            _wb_log(f"Memory modal: dismissed via CV (score={score:.3f}).")
            return True
    except Exception:
        pass

    # 3) ESC-Fallback
    with suppress(Exception):
        page.keyboard.press("Escape")
        page.wait_for_timeout(80)

    return False


# -------------------- Prompt I/O (Composer) --------------------
def composer_is_focused(page) -> bool:
    try:
        return page.evaluate("""() => {
            const el = document.querySelector('[role="textbox"][contenteditable="true"]')
                   || document.querySelector('textarea');
            if (!el) return false;
            return document.activeElement === el;
        }""")
    except Exception:
        return False

def focus_composer_dom(page) -> bool:
    try:
        ok = page.evaluate("""() => {
            const el = document.querySelector('[role="textbox"][contenteditable="true"]')
                   || document.querySelector('textarea');
            if (!el) return false;
            el.focus();
            if (el.isContentEditable) {
                const sel = window.getSelection();
                const r = document.createRange(); r.selectNodeContents(el); r.collapse(false);
                sel.removeAllRanges(); sel.addRange(r);
            }
            return document.activeElement === el;
        }""")
        return bool(ok)
    except Exception:
        return False

def _is_safe_to_submit(page) -> bool:
    try:
        return page.evaluate("""() => {
          const el = document.activeElement;
          if (!el) return false;
          const inComposer = el.matches('[role="textbox"][contenteditable="true"]') || el.tagName === 'TEXTAREA';
          const inSearchBar = !!el.closest('[role="search"], [role="searchbox"], [aria-label*="Search the web" i]');
          return inComposer && !inSearchBar;
        }""")
    except Exception:
        return False

def _maybe_press_enter(page):
    if _is_safe_to_submit(page):
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass

def send_prompt(page, cfg, text, *, auto_submit: bool = False) -> None:
    if not composer_is_focused(page):
        focus_composer_dom(page)
        time.sleep(0.05)

    try:
        page.keyboard.insert_text(text)
    except Exception:
        page.keyboard.type(text, delay=float(getattr(cfg, "type_delay_ms", 0.0)))

    if auto_submit and _is_safe_to_submit(page):
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass

    time.sleep(float(getattr(cfg, "mouse_after_click_sleep", 0.18)))

def send_prompt_v2(page, cfg, text, *, auto_submit: bool = False) -> None:
    if not composer_is_focused(page):
        focus_composer_dom(page)
        time.sleep(0.05)

    try:
        page.keyboard.insert_text(text)
    except Exception:
        page.keyboard.type(text, delay=float(getattr(cfg, "type_delay_ms", 0.0)))

    if auto_submit and _is_safe_to_submit(page):
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass

    time.sleep(float(getattr(cfg, "mouse_after_click_sleep", 0.18)))

def _extract_latest_json_from_html(html: str) -> Optional[Dict[str, Any]]:
    # 1) klassisch: ```json ... ```
    m = list(re.finditer(r"```json\s*(\{.*?\})\s*```", html, re.DOTALL | re.IGNORECASE))
    if m:
        block = m[-1].group(1)
        try:
            return json.loads(block)
        except Exception:
            with suppress(Exception):
                return json.loads(block.replace("\u201c", '"').replace("\u201d", '"'))
    # 2) Fallback: irgendein JSON „sichtbar“ im HTML
    m2 = re.search(r"\{[\s\S]*\}", html)
    if m2:
        return _loose_json_parse(m2.group(0))
    return None

def wait_for_json_codeblock(page, *, total_timeout_s: int = 75, settle_s: float = 0.6) -> Optional[Dict[str, Any]]:
    """
    Pollt DOM-first (pre>code), danach HTML-Fallback. Liefert das letzte stabile JSON.
    """
    t0 = time.time()
    last_found = None  # (t_stamp, dict)

    while (time.time() - t0) < float(total_timeout_s):
        # 1) DOM-first
        data = _extract_latest_json_from_dom(page)
        if data is None:
            # 2) HTML-Fallback
            with suppress(Exception):
                html = page.content()
                data = _extract_latest_json_from_html(html)

        if isinstance(data, dict):
            if last_found is None:
                last_found = (time.time(), data)
            else:
                # „settle“ – gleiches/erneut JSON für settle_s Sekunden -> stabil
                if (time.time() - last_found[0]) >= float(settle_s):
                    return data
        time.sleep(0.30)

    return None

def _loose_json_parse(txt: str) -> Optional[Dict[str, Any]]:
    t = (txt or "").strip()
    if not t:
        return None
    # Entferne evtl. Fences/Prefix
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
        t = re.sub(r"\s*```$", "", t)
    # Nimm Teil zwischen erster { und letzter }
    s, e = t.find("{"), t.rfind("}")
    if s >= 0 and e > s:
        cand = t[s:e+1]
        try:
            return json.loads(cand)
        except Exception:
            # gerade Anführungszeichen erzwingen
            cand2 = cand.replace("\u201c", '"').replace("\u201d", '"')
            try:
                return json.loads(cand2)
            except Exception:
                return None
    return None

def _extract_latest_json_from_dom(page) -> Optional[Dict[str, Any]]:
    """
    Nimmt den letzten Codeblock (<pre><code> …) und prüft, ob darin JSON steht.
    """
    try:
        blocks: List[str] = page.evaluate("""() => {
          const nodes = Array.from(document.querySelectorAll('pre code, code'));
          return nodes.map(n => n.innerText || n.textContent || '');
        }""")
    except Exception:
        blocks = []
    for raw in reversed(blocks or []):
        d = _loose_json_parse(raw)
        if isinstance(d, dict):
            return d
    return None
