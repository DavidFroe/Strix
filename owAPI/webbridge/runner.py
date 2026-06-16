# webbridge/runner.py
import os, re, time, json, subprocess, urllib.request, urllib.error
from contextlib import suppress
from typing import Dict, Tuple, Iterable, Optional, List

from playwright.sync_api import TimeoutError as PwTimeout

# config type for hints only
try:
    from webbridge.config import BridgeConfig
except Exception:
    class BridgeConfig:  # fallback duck-type
        pass

from webbridge.click_layer import ClickLayer, ClickRequest, ClickMarkup

# Optional window helper
try:
    import pygetwindow as gw
except Exception:
    gw = None

def log(msg: str):
    print(str(msg), flush=True)

# ------------------- Chrome / CDP -------------------

def _port_open(addr: str, port: int, timeout_s: float = 0.5) -> bool:
    try:
        urllib.request.urlopen(f"http://{addr}:{port}/json/version", timeout=timeout_s).read()
        return True
    except Exception:
        return False

def start_chrome_debug(cfg: BridgeConfig):
    """Start Chrome with remote debugging port, if not already running."""
    log("[bridge] Starte Chrome mit Debug-Port …")
    args = [
        f"--remote-debugging-address={cfg.cdp_addr}",
        f"--remote-debugging-port={cfg.cdp_port}",
        f"--user-data-dir={cfg.chrome_user_data_dir}",
        "about:blank"
    ]
    subprocess.Popen([cfg.chrome_exe, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Wait for CDP port to become available
    deadline = time.time() + getattr(cfg, "wait_cdp_seconds", 10)
    while time.time() < deadline:
        if _port_open(cfg.cdp_addr, cfg.cdp_port):
            log("[bridge] CDP erreichbar.")
            return
        time.sleep(0.3)
    raise RuntimeError("Chrome-CDP wurde nicht erreichbar (Timeout).")

def connect_chrome(pw, cfg: BridgeConfig):
    """Connect to an existing Chrome instance via CDP, or raise if not available."""
    try:
        ver_data = urllib.request.urlopen(f"http://{cfg.cdp_addr}:{cfg.cdp_port}/json/version", timeout=2.0).read()
        ver_json = json.loads(ver_data.decode("utf-8"))
        ws_url = ver_json.get("webSocketDebuggerUrl") or ver_json.get("webSocketDebuggerUrl".lower())
        if not ws_url:
            raise RuntimeError("webSocketDebuggerUrl fehlt in /json/version")
    except Exception as e:
        raise RuntimeError(f"CDP /json/version nicht erreichbar: {e}")

    browser = pw.chromium.connect_over_cdp(f"http://{cfg.cdp_addr}:{cfg.cdp_port}")
    log("[bridge] CDP verbunden.")
    return browser

def pick_or_open_chat_page(browser, chat_url: str):
    """Pick a tab with chat url or open new."""
    for ctx in browser.contexts:
        for p in ctx.pages:
            with suppress(Exception):
                if chat_url.split("//",1)[-1].split("/",1)[0] in (p.url or ""):
                    log("[bridge] Verwende existierenden Chat-Tab.")
                    return p
    log("[bridge] Neuer Tab wird geöffnet …")
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    p = ctx.new_page()
    p.goto(chat_url, wait_until="domcontentloaded", timeout=10_000)
    return p

# ------------------- Page utilities -------------------

def accept_cookies(page):
    """Click common 'Accept' buttons for cookies or terms."""
    texts = ["Accept all", "Akzeptieren", "Ich stimme zu", "Allow all", "Alle akzeptieren", "Zustimmen"]
    for txt in texts:
        with suppress(Exception):
            btn = page.locator(f"button:has-text('{txt}')")
            if btn.count() > 0:
                btn.first.click()
                time.sleep(0.2)

def nuke_overlays(page):
    """Try to dismiss overlays and sidebars that might block clicks."""
    try:
        page.keyboard.press("Escape")
        time.sleep(0.1)
        page.evaluate("""() => {
            for (const sel of ['[aria-label="Close"]','[data-testid*="close"]','.close']) {
                document.querySelectorAll(sel).forEach(el => { try{el.click()}catch(e){} });
            }
        }""")
    except Exception:
        pass

def on_login_page(page) -> bool:
    url = ""
    with suppress(Exception):
        url = page.url or ""
    return any(s in url for s in ["auth0", "accounts.google.com", "login"])

def ensure_composer_or_recover(page, cfg: BridgeConfig, *, new_chat: bool=False) -> bool:
    """Ensure the message composer is reachable; try basic recoveries.
    'new_chat' wird aktuell nicht benötigt, ist aber für API-Kompatibilität vorhanden.
    """
    for _ in range(2):
        try:
            accept_cookies(page)
            with suppress(Exception):
                page.wait_for_selector("[data-testid^='composer']", timeout=1500)
                return True
            # click into bottom area to focus
            vi = page.evaluate("() => ({iw: innerWidth, ih: innerHeight})")
            page.mouse.click(int(vi["iw"]*0.5), int(vi["ih"]*0.92))
            with suppress(Exception):
                page.wait_for_selector("[data-testid^='composer']", timeout=1200)
                return True
        except PwTimeout:
            pass
        except Exception:
            pass
        nuke_overlays(page)
        time.sleep(0.3)
    return False

def _web_search_active(page) -> bool:
    with suppress(Exception):
        el = page.locator("[data-testid*='web'] [data-state='on'], [aria-pressed='true']:has-text('Web')")
        if el.count() > 0:
            return True
    with suppress(Exception):
        pills = page.locator("[data-testid*='tools'], [role='toolbar'] button")
        if pills.count() > 2:
            return True
    return False

def enable_temporary_chat(page, cfg: BridgeConfig) -> bool:
    """Try to enable temporary chat via DOM, return True if already enabled or set."""
    with suppress(Exception):
        sw = page.get_by_role("switch", name=re.compile("Temporary|Temporär", re.I))
        if sw.count() > 0:
            sw.first.click()
            time.sleep(0.25)
            return True
    return False

def enable_web_search(page, cfg: BridgeConfig) -> bool:
    """Versuche die Websuche rein per DOM zu aktivieren (ohne Koordinaten)."""
    # Plus öffnen (Explizit über data-testid, dann über Rolle)
    with suppress(Exception):
        btn = page.locator("[data-testid='composer-plus-btn']")
        if btn.count() > 0:
            btn.first.click()
            time.sleep(0.25)
    with suppress(Exception):
        plus = page.get_by_role("button", name=re.compile(r"^\+$"))
        if plus.count() > 0:
            plus.first.click()
            time.sleep(0.25)

    # Kandidaten für "Websuche"
    patterns = [r"Web ?search", r"Websuche", r"Im Web suchen", r"Search the web"]
    # Direkt-Buttons
    for pat in patterns:
        with suppress(Exception):
            b = page.get_by_role("button", name=re.compile(pat, re.I))
            if b.count() > 0:
                b.first.click()
                time.sleep(0.25)
                return True

    # Menü/Popover durchsuchen
    with suppress(Exception):
        ok = page.evaluate("""(pats) => {
            const roots = [...document.querySelectorAll('[role="dialog"],[data-testid*="popover"],[role="menu"]')];
            roots.push(document.body);
            for (const root of roots) {
                const els = [...root.querySelectorAll('*')];
                for (const el of els) {
                    const t = (el.innerText || el.ariaLabel || '').trim();
                    for (const pat of pats) {
                        const re = new RegExp(pat, 'i');
                        if (re.test(t)) {
                            const btn = el.closest('button') || el.querySelector('button');
                            if (btn) { btn.click(); return true; }
                            el.click(); return true;
                        }
                    }
                }
            }
            return false;
        }""", patterns)
        if ok:
            time.sleep(0.25)
            return True

    return _web_search_active(page)

# ------------------- New click-backed fallback -------------------
from webbridge.click_layer import ClickLayer  # sicherstellen, dass der Import oben steht

def mouse_fallback_enable_web_search(
    page,
    cfg: BridgeConfig,
    retries: int = 1,
    *,
    mode: str,
    abs_coords: Dict[str, Tuple[float, float]],
    rel_coords: Dict[str, Tuple[float, float]],
) -> bool:
    """
    Minimal-Fallback:
      1) PLUS klicken (Koordinate)
      2) MORE hovern (aktives Hover + Jitter)
      3) WEB exakt an Koordinate klicken (z. B. 1051,768)
    Keine DOM-/Bild-/Relativ-Experimente.
    """
    cl = ClickLayer(page, cfg)

    # Wir machen GENAU EINEN deterministischen Versuch.
    ok = cl.hover_plus_more_then_click_web_coords_only(
        mode=mode,
        abs_coords=abs_coords,
        rel_coords=rel_coords,
        hover_backend=getattr(cfg, "hover_backend", "os"),
    )
    return bool(ok)



# ------------------- Composer helpers -------------------

def find_composer(page):
    """
    Finde die echte Eingabe (contenteditable/textarea), nicht den Plus-Button.
    Gibt einen Locator zurück oder None.
    """
    # 1) Exakte data-testid-Varianten (häufig)
    with suppress(Exception):
        loc = page.locator("[data-testid='composer:input']")
        if loc.count() > 0:
            return loc.first
    with suppress(Exception):
        loc = page.locator("[data-testid='composer'] [data-testid='composer:input']")
        if loc.count() > 0:
            return loc.first

    # 2) contenteditable innerhalb Composer-Container
    with suppress(Exception):
        loc = page.locator("[data-testid='composer'] [role='textbox'][contenteditable='true'], [data-testid='composer'] div[contenteditable='true'][role='textbox']")
        if loc.count() > 0:
            return loc.first

    # 3) globale contenteditable-Textbox (fallback)
    with suppress(Exception):
        loc = page.locator("[role='textbox'][contenteditable='true'], div[contenteditable='true'][role='textbox']")
        if loc.count() > 0:
            return loc.first

    # 4) textarea-Fallbacks
    with suppress(Exception):
        loc = page.locator("[data-testid='composer'] textarea, textarea[name='prompt-textarea']")
        if loc.count() > 0:
            return loc.first

    return None


def dom_focus_composer(page) -> bool:
    """
    Versuche, den Composer per DOM in den Fokus zu bringen.
    """
    loc = find_composer(page)
    if not loc:
        return False
    with suppress(Exception):
        loc.scroll_into_view_if_needed(timeout=1000)
    with suppress(Exception):
        loc.focus(timeout=1000)
        return True
    # letzte Chance: direkt klicken
    with suppress(Exception):
        box = loc.bounding_box()
        if box:
            page.mouse.click(int(box["x"] + box["width"]*0.5), int(box["y"] + box["height"]*0.5))
            return True
    return False


def mouse_focus_composer(page, cfg) -> bool:
    """
    Fokussiere blind den unteren Eingabebereich per Maus.
    Nutzt Viewport (50%, 92%) – robust gegen verschiedene Layouts.
    """
    try:
        vi = page.evaluate("() => ({iw: innerWidth, ih: innerHeight})")
        x = int(vi["iw"] * 0.50)
        y = int(vi["ih"] * 0.92)
        page.mouse.click(x, y)
        time.sleep(0.15)
        return True
    except Exception:
        return False


def send_prompt(page, cfg: BridgeConfig, text: str):
    """Text sicher in den Composer bringen und senden (ohne 'fill' auf Buttons)."""
    # 1) Composer sichern
    accept_cookies(page)
    if not dom_focus_composer(page):
        if not mouse_focus_composer(page, cfg):
            # ein zweiter Versuch mit kurzen Overlays-Cleanup
            nuke_overlays(page)
            if not dom_focus_composer(page) and not mouse_focus_composer(page, cfg):
                raise RuntimeError("Composer nicht fokussierbar.")

    # 2) Text eingeben – bevorzugt via Tastatur (funktioniert bei contenteditable stabil)
    try:
        page.keyboard.insert_text(text)
    except Exception:
        # Fallbacks: direkt in contenteditable/textarea schreiben
        with suppress(Exception):
            ok = page.evaluate("""(t) => {
                const c = document.querySelector("[data-testid='composer:input']") 
                       || document.querySelector("[data-testid='composer'] [data-testid='composer:input']") 
                       || document.querySelector("[data-testid='composer'] [role='textbox'][contenteditable='true']") 
                       || document.querySelector("div[role='textbox'][contenteditable='true']");
                if (c) {
                    c.focus();
                    try { document.execCommand('selectAll', false, null); } catch(e) {}
                    try { document.execCommand('insertText', false, t); return true; } catch(e) {}
                }
                const ta = document.querySelector("[data-testid='composer'] textarea, textarea[name='prompt-textarea']");
                if (ta) { ta.value = t; ta.dispatchEvent(new Event('input', {bubbles:true})); return true; }
                return false;
            }""", text)
            if not ok:
                # noch ein sehr simpler Notnagel
                loc = find_composer(page)
                if loc and loc.count() > 0:
                    loc.type(text, delay=0)

    # 3) Absenden
    with suppress(Exception):
        page.keyboard.press("Enter")




def wait_for_json_codeblock(page, total_timeout_s: int = 20, settle_s: float = 0.6) -> Optional[dict]:
    """
    Warte bis ein JSON-Codeblock erscheint und PARSE ihn zu einem dict.
    - total_timeout_s: Gesamtwartezeit (Sekunden)
    - settle_s: kleine Zusatzwartezeit nach Fund, um UI zu stabilisieren
    """
    deadline = time.time() + float(total_timeout_s)
    code_re = re.compile(r"```json\s*(\{[\s\S]*?\})\s*```", re.I)
    last_html = ""
    captured = None

    while time.time() < deadline:
        with suppress(Exception):
            html = page.inner_html("[data-testid^='conversation']")
            if html and html != last_html:
                last_html = html
                m = code_re.search(html)
                if m:
                    captured = m.group(1)
                    break
        time.sleep(0.4)

    if not captured:
        return None

    # kleine Settle-Zeit, dann ggf. letzten sichtbaren Codeblock prüfen
    time.sleep(max(0.0, float(settle_s)))
    with suppress(Exception):
        blocks = page.locator("pre").all()
        if blocks:
            txt = blocks[-1].inner_text()
            m2 = code_re.search(txt)
            if m2:
                captured = m2.group(1)

    try:
        return json.loads(captured)
    except Exception:
        # Fallback: hart parsebaren Bereich suchen (geschweifte Klammern balancieren)
        with suppress(Exception):
            return json.loads(captured.strip())
        return None
