# webbridge/composer_io.py
from __future__ import annotations
from contextlib import suppress

from .web_adapter import composer_is_focused, focus_composer_dom

def _active_text(page) -> str:
    try:
        return page.evaluate("""() => {
          const el = document.activeElement;
          if (!el) return '';
          if (el.isContentEditable) return el.innerText || '';
          if ('value' in el) return el.value || '';
          return '';
        }""")
    except Exception:
        return ""

def focus_composer(page) -> str:
    """
    Sichert Fokus zuverlässig.
    Rückgabe: 'already' | 'js' | 'click' | 'fail'
    """
    try:
        if composer_is_focused(page):
            return "already"

        # 1) Direkter JS-Fokus auf bekannte Targets
        ok = page.evaluate("""() => {
            const pick = () => {
              // bekannte Kandidaten nach Priorität
              const byId = document.getElementById('prompt-textarea');
              if (byId) return byId;

              const roleTb = document.querySelector('[role="textbox"][contenteditable="true"]');
              if (roleTb) return roleTb;

              const inComposerCE = document.querySelector('[data-testid^="composer"] [contenteditable="true"]');
              if (inComposerCE) return inComposerCE;

              const ta = document.querySelector('textarea[placeholder*="Message" i], textarea');
              if (ta) return ta;

              const prose = [...document.querySelectorAll('.ProseMirror[contenteditable="true"]')][0];
              if (prose) return prose;

              return null;
            };
            const el = pick();
            if (!el) return false;
            if (el.isContentEditable) {
              el.focus();
              try {
                const sel = window.getSelection();
                const r = document.createRange(); r.selectNodeContents(el); r.collapse(false);
                sel.removeAllRanges(); sel.addRange(r);
              } catch(_) {}
            } else {
              el.focus();
              try { el.scrollIntoView({block:'end'}); } catch(_){}
            }
            return document.activeElement === el;
        }""")
        if ok:
            page.wait_for_timeout(40)
            return "js"

        # 2) Klick-Fallback ins Composer-Areal
        with suppress(Exception):
            page.evaluate("""() => {
              const host = document.querySelector('[data-testid^="composer"]')
                        || document.querySelector('#prompt-textarea')
                        || document.querySelector('[role="textbox"][contenteditable="true"]')
                        || document.querySelector('textarea') || document.body;
              host.scrollIntoView({block:'end'});
            }""")
        with suppress(Exception):
            # Leicht untere Mitte anklicken
            box = page.evaluate("""() => {
              const host = document.querySelector('[data-testid^="composer"]')
                        || document.querySelector('#prompt-textarea')
                        || document.querySelector('[role="textbox"][contenteditable="true"]')
                        || document.querySelector('textarea');
              if (!host) return null;
              const r = host.getBoundingClientRect();
              return { x: (r.left + r.right)/2, y: (r.top + r.bottom)/2 + Math.min(18, (r.bottom-r.top)/4) };
            }""")
            if box and isinstance(box, dict) and "x" in box and "y" in box:
                page.mouse.move(int(box["x"]), int(box["y"]), steps=4)
                page.mouse.click(int(box["x"]), int(box["y"]))
                page.wait_for_timeout(30)
                if composer_is_focused(page):
                    return "click"
    except Exception:
        pass
    return "fail"


def feed_peck_check(page) -> bool:
    """Marker schreiben/lesen und wieder löschen – ohne Enter."""
    marker = "§§KI_FEEDPECK_§§"
    try:
        page.keyboard.insert_text(marker)
        got = _active_text(page)
        ok = (marker in got)
    except Exception:
        ok = False

    with suppress(Exception):
        page.keyboard.down("Control"); page.keyboard.press("KeyA"); page.keyboard.up("Control")
        page.keyboard.press("Backspace")

    if not ok:
        with suppress(Exception):
            page.evaluate("""() => { const el = document.activeElement;
              if (!el) return;
              if (el.isContentEditable) el.innerText = '';
              else if ('value' in el) { el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); } }""")
            ok = True
    return ok

def insert_prompt(page, text: str) -> bool:
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
              else if ('value' in el) { el.value = t; el.dispatchEvent(new Event('input',{bubbles:true})); }
              else { throw new Error('active not writable'); }
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
    return ok

def safe_submit(page, expect_prefix: str = "", allow_soft_fallback: bool = True) -> bool:
    """
    Drückt Enter nur, wenn unser Text wirklich (nahezu) im Composer steht.
    Soft-Fallback: wenn Composer fokussiert ist und Text nicht leer, Enter trotzdem.
    """
    try:
        txt = _active_text(page) or ""
        # Toleranter Check (Whitespace normalisieren)
        def norm(s: str) -> str:
            return " ".join((s or "").strip().split())[:160]
        ours = False
        if expect_prefix:
            ours = norm(expect_prefix) in norm(txt)
        else:
            ours = "SYSTEMROLLE: OADS2" in txt

        if not ours and allow_soft_fallback:
            # Fallback: wenn Composer fokussiert und etwas Text vorhanden → Enter trotzdem
            try:
                if composer_is_focused(page) and len(norm(txt)) > 0:
                    page.keyboard.press("Enter")
                    return True
            except Exception:
                pass

        if not ours:
            return False

        page.keyboard.press("Enter")
        return True
    except Exception:
        return False

