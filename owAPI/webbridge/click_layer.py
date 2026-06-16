# webbridge/click_layer.py
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Dict, Tuple, Iterable, Optional, List, Any
from contextlib import suppress
from pathlib import Path
import re, time, os, json

# -------- Optional deps (robust gegen fehlende Pakete) --------
try:
    import pyautogui
    try:
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.0
    except Exception:
        pass
except Exception:
    pyautogui = None

try:
    import pygetwindow as gw
except Exception:
    gw = None

try:
    import cv2  # type: ignore
    import numpy as np  # type: ignore
except Exception:
    cv2 = None
    np = None


def _log(msg: str):
    print(f"[click] {msg}", flush=True)


# ---------------------- Data-Model ----------------------
@dataclass
class ClickMarkup:
    name: str = ""
    text: str = ""                                # Regex oder Plaintext
    role: str = "button"                          # "button" | "link" | "switch" | "any"
    selectors: List[str] = field(default_factory=list)
    img_path: Optional[str] = None                # Template-Bild für Bild-Gate
    must_be_visible: bool = True
    expected_box_css: Optional[Tuple[int,int,int,int]] = None   # (x1,y1,x2,y2)
    expected_box_rel: Optional[Tuple[float,float,float,float]] = None
    min_score: Optional[float] = None             # override (0..1), sonst cfg.image_match_threshold


@dataclass
class ClickRequest:
    rel: Optional[Tuple[float, float]] = None     # relative (0..1) Viewport-Koord.
    css: Optional[Tuple[int, int]] = None         # CSS-Pixel
    screen: Optional[Tuple[int, int]] = None      # Screen-Pixel
    markup: List[ClickMarkup] = field(default_factory=list)
    allow_maximize: bool = True                   # Rettungsmaßnahme


# ---------------------- ClickLayer ----------------------
class ClickLayer:
    """
    Vereinheitlichte Klick-Schicht:
      - Relativ-/CSS-/Screen-Koordinaten
      - DOM/ARIA und CSS-Selektoren
      - Echte Maus (PyAutoGUI) für Hover/Bewegung
      - Bild-Gate via OpenCV (optional, mit Regionsbegrenzung)
      - Umfangreicher Debug-Trace + Screenshots
    """

    def __init__(self, page, cfg):
        self.page = page
        self.cfg = cfg
        self._trace: List[Dict[str, Any]] = []
        self.debug_on = bool(getattr(cfg, "debug_clicks", False) or os.getenv("WB_DEBUG_CLICKS"))
        self.debug_dir = Path(getattr(cfg, "debug_dir", "debug"))
        if self.debug_on:
            self.debug_dir.mkdir(parents=True, exist_ok=True)

        self._hover_backend = os.getenv("WB_HOVER_BACKEND") or getattr(cfg, "hover_backend", None) or ("os" if pyautogui else "playwright")
        self._mouse_move_duration = float(getattr(cfg, "mouse_move_duration", 0.06))
        self._after_click_sleep   = float(getattr(cfg, "mouse_after_click_sleep", 0.12))
        self._image_match_threshold = float(getattr(cfg, "image_match_threshold", 0.86))
        self._title_hint = str(getattr(cfg, "window_title_hint", "Chrome"))

    # ---------------------- Debug ----------------------
    def _trace_add(self, kind: str, data: Dict[str, Any]):
        if self.debug_on:
            d = dict(data)
            d["kind"] = kind
            d["ts"] = time.time()
            self._trace.append(d)

    def _debug_dump(self, label: str):
        if not self.debug_on:
            return
        try:
            payload = {
                "label": label,
                "viewport": self.viewport_info(),
                "hover_backend": self._hover_backend,
                "trace": self._trace,
            }
            out = self.debug_dir / f"click_trace_{int(time.time()*1000)}_{label}.json"
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            _log(f"debug trace saved: {out}")
        except Exception as e:
            _log(f"debug dump failed: {e}")

    def _screenshot(self, label: str) -> Optional[Path]:
        if not self.debug_on:
            return None
        try:
            p = self.debug_dir / f"shot_{int(time.time()*1000)}_{label}.png"
            self.page.screenshot(path=str(p), full_page=False)
            self._trace_add("screenshot", {"path": str(p)})
            return p
        except Exception as e:
            self._trace_add("screenshot_error", {"error": str(e)})
            return None

    # ---------------------- Geometrie ----------------------
    def viewport_info(self) -> Dict[str, Any]:
        try:
            return self.page.evaluate("""() => ({
                dpr: window.devicePixelRatio || 1,
                iw: window.innerWidth, ih: window.innerHeight,
                sx: window.screenX || 0, sy: window.screenY || 0
            })""")
        except Exception:
            return {"dpr": 1.0, "iw": 1024, "ih": 768, "sx": 0, "sy": 0}

    def rel_to_css(self, rel_xy: Tuple[float, float]) -> Tuple[int, int]:
        v = self.viewport_info()
        x = int(round(max(0.0, min(1.0, float(rel_xy[0]))) * v["iw"]))
        y = int(round(max(0.0, min(1.0, float(rel_xy[1]))) * v["ih"]))
        self._trace_add("rel_to_css", {"rel": rel_xy, "css": (x, y)})
        return x, y

    def screen_to_css(self, screen_xy: Tuple[int, int], offset: Tuple[int, int] = (0, 0)) -> Tuple[int, int]:
        v = self.viewport_info()
        dpr = max(1.0, float(v.get("dpr", 1.0)))
        sx, sy = int(screen_xy[0]) - int(offset[0]), int(screen_xy[1]) - int(offset[1])
        x = int(round(sx / dpr))
        y = int(round(sy / dpr))
        self._trace_add("screen_to_css", {"screen": screen_xy, "offset": offset, "dpr": dpr, "css": (x, y)})
        return x, y

    def css_to_screen(self, css_xy: Tuple[int, int], offset: Tuple[int, int] = (0, 0)) -> Tuple[int, int]:
        v = self.viewport_info()
        dpr = max(1.0, float(v.get("dpr", 1.0)))
        sx = int(round(css_xy[0] * dpr + offset[0]))
        sy = int(round(css_xy[1] * dpr + offset[1]))
        self._trace_add("css_to_screen", {"css": css_xy, "offset": offset, "dpr": dpr, "screen": (sx, sy)})
        return sx, sy

    # ---------------------- Bewegung / Hover ----------------------
    def move_mouse(self, x:int, y:int, *, backend:str="auto", duration:float=None):
        backend = backend or self._hover_backend or "auto"
        duration = self._mouse_move_duration if duration is None else float(duration)
        self._trace_add("move_mouse_begin", {"x": x, "y": y, "backend": backend, "duration": duration})
        ok = False

        if backend in ("playwright", "auto"):
            try:
                steps = max(1, int(duration * 60))
                self.page.mouse.move(x, y, steps=steps)
                ok = True
                self._trace_add("move_mouse_pw", {"ok": True})
            except Exception as e:
                self._trace_add("move_mouse_pw", {"ok": False, "error": str(e)})

        if (backend in ("os", "auto")) and (pyautogui is not None) and not ok:
            try:
                off = (0,0)
                so = os.getenv("WB_SCREEN_OFFSET")
                if so and "," in so:
                    with suppress(Exception):
                        ox, oy = so.split(",", 1)
                        off = (int(ox), int(oy))
                sx, sy = self.css_to_screen((x, y), offset=off)
                pyautogui.moveTo(sx, sy, duration=max(0.01, duration))
                ok = True
                self._trace_add("move_mouse_os", {"ok": True, "screen": (sx, sy)})
            except Exception as e:
                self._trace_add("move_mouse_os", {"ok": False, "error": str(e)})

        time.sleep(0.02)
        return ok

    def hover(self, x:int, y:int, *, backend:str=None, dwell:float=0.30, jiggle:bool=True):
        backend = backend or self._hover_backend
        ok = self.move_mouse(x, y, backend=backend, duration=max(0.08, self._mouse_move_duration))
        if jiggle:
            with suppress(Exception):
                self.move_mouse(x+1, y, backend=backend, duration=0.04)
                self.move_mouse(x,   y, backend=backend, duration=0.04)
        time.sleep(max(0.02, dwell))
        self._trace_add("hover_done", {"x": x, "y": y, "backend": backend, "dwell": dwell, "jiggle": jiggle})
        return ok

    def _is_markup_visible(self, mk: ClickMarkup) -> bool:
        try:
            if mk.text:
                loc = self.page.get_by_role(mk.role or "button", name=re.compile(mk.text, re.I))
                if loc.count() > 0 and loc.first.is_visible():
                    return True
            for sel in mk.selectors:
                loc = self.page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return True
        except Exception:
            pass
        return False

    def hover_force(self, x:int, y:int, *,
                    backend:str=None,
                    enter_dx:int=None, enter_dy:int=None,
                    wiggle_px:int=None, cycles:int=None,
                    dwell:float=None):
        backend   = backend   or getattr(self.cfg, "hover_backend", "auto")
        enter_dx  = enter_dx  if enter_dx  is not None else int(getattr(self.cfg, "hover_enter_dx", -120))
        enter_dy  = enter_dy  if enter_dy  is not None else int(getattr(self.cfg, "hover_enter_dy", 0))
        wiggle_px = wiggle_px if wiggle_px is not None else int(getattr(self.cfg, "hover_wiggle_px", 6))
        cycles    = cycles    if cycles    is not None else int(getattr(self.cfg, "hover_wiggle_cycles", 2))
        dwell     = dwell     if dwell     is not None else float(getattr(self.cfg, "hover_dwell_more", 0.35))

        with suppress(Exception):
            self.move_mouse(x + enter_dx, y + enter_dy, backend=backend, duration=max(0.08, self._mouse_move_duration))
        self.move_mouse(x, y, backend=backend, duration=max(0.10, self._mouse_move_duration))

        for _ in range(max(1, cycles)):
            with suppress(Exception):
                self.move_mouse(x + wiggle_px, y, backend=backend, duration=0.04)
                self.move_mouse(x - wiggle_px, y, backend=backend, duration=0.04)
                self.move_mouse(x, y, backend=backend, duration=0.03)

        time.sleep(max(0.05, dwell))
        self._trace_add("hover_force_done", {
            "x": x, "y": y, "backend": backend,
            "enter_dx": enter_dx, "enter_dy": enter_dy,
            "wiggle_px": wiggle_px, "cycles": cycles, "dwell": dwell
        })
        return True

    # ---------------------- Low-level Clicks ----------------------
    def _flash_marker(self, x: int, y: int):
        with suppress(Exception):
            self.page.evaluate("""(x,y) => {
                const d = document.createElement('div');
                Object.assign(d.style, {
                    position:'fixed', left:(x-6)+'px', top:(y-6)+'px',
                    width:'12px', height:'12px', borderRadius:'50%',
                    background:'rgba(0,180,255,0.65)', boxShadow:'0 0 0 2px white',
                    zIndex: 2147483647, pointerEvents:'none', transition:'opacity .35s'
                });
                document.body.appendChild(d);
                setTimeout(()=>{ d.style.opacity='0'; setTimeout(()=>d.remove(), 350); }, 400);
            }""", x, y)

    def _peek_element_at(self, x:int, y:int) -> Optional[Dict[str,str]]:
        try:
            return self.page.evaluate("""(x,y) => {
                const el = document.elementFromPoint(Math.round(x), Math.round(y));
                if (!el) return null;
                return {
                    tag: (el.tagName||'').toLowerCase(),
                    id: el.id || '',
                    cls: (typeof el.className==='string' ? el.className : ''),
                    aria: el.getAttribute?.('aria-label') || '',
                    txt: (el.innerText || el.textContent || '').trim().slice(0,120)
                };
            }""", x, y)
        except Exception:
            return None

    def _pw_click(self, x:int, y:int) -> bool:
        try:
            self._flash_marker(x, y)
            steps = max(1, int(self._mouse_move_duration * 60))
            self.page.mouse.move(x, y, steps=steps)
            self.page.mouse.click(x, y, delay=20)
            time.sleep(self._after_click_sleep)
            self._trace_add("pw_click", {"css": (x, y), "ok": True})
            return True
        except Exception as e:
            self._trace_add("pw_click", {"css": (x, y), "ok": False, "error": str(e)})
            return False

    def _pya_click(self, css_x:int, css_y:int) -> bool:
        if pyautogui is None:
            self._trace_add("pyautogui_absent", {})
            return False
        try:
            off = (0,0)
            so = os.getenv("WB_SCREEN_OFFSET")
            if so and "," in so:
                with suppress(Exception):
                    ox, oy = so.split(",", 1)
                    off = (int(ox), int(oy))
            sx, sy = self.css_to_screen((css_x, css_y), offset=off)
            pyautogui.moveTo(sx, sy, duration=self._mouse_move_duration)
            pyautogui.click()
            time.sleep(self._after_click_sleep)
            self._trace_add("pyautogui_click", {"screen": (sx, sy), "css": (css_x, css_y), "ok": True})
            return True
        except Exception as e:
            self._trace_add("pyautogui_click", {"css": (css_x, css_y), "ok": False, "error": str(e)})
            return False

    # ---------------------- DOM-Clicks ----------------------
    def _dom_click_by_markup(self, mk: ClickMarkup) -> bool:
        pattern = mk.text or ""
        role = mk.role or "button"

        # 1) Rolle/Name
        with suppress(Exception):
            if role != "any" and pattern:
                loc = self.page.get_by_role(role, name=re.compile(pattern, re.I))
                if loc.count() > 0:
                    loc.first.click()
                    time.sleep(0.35)
                    self._trace_add("dom_click_role", {"role": role, "pattern": pattern, "ok": True})
                    return True

        # 2) Selektoren
        for sel in mk.selectors:
            with suppress(Exception):
                loc = self.page.locator(sel)
                if loc.count() > 0:
                    if mk.must_be_visible:
                        with suppress(Exception):
                            loc.first.scroll_into_view_if_needed(timeout=1000)
                    loc.first.click()
                    time.sleep(0.35)
                    self._trace_add("dom_click_selector", {"selector": sel, "ok": True})
                    return True

        # 3) Dialog/Popover/Menu Scan
        if pattern:
            with suppress(Exception):
                ok = self.page.evaluate("""(regex, role) => {
                    const re = new RegExp(regex, 'i');
                    const roots = [...document.querySelectorAll('[role="dialog"],[data-testid*="popover"],[role="menu"],.popover,.popup')];
                    roots.push(document.body);
                    for (const root of roots) {
                        const els = [...root.querySelectorAll('*')];
                        for (const el of els) {
                            const t = (el.innerText || el.ariaLabel || '').trim();
                            if (re.test(t)) {
                                if (role && role!=='any') {
                                    const rsel = role==='button' ? 'button' : `[role="${role}"]`;
                                    const c = el.closest(rsel) || el.querySelector(rsel);
                                    if (c) { c.click(); return true; }
                                }
                                const btn = el.closest('button') || el.querySelector('button');
                                const sw  = el.closest('[role="switch"]') || el.querySelector('[role="switch"]');
                                if (sw) { sw.click(); return true; }
                                if (btn){ btn.click(); return true; }
                                el.click(); return true;
                            }
                        }
                    }
                    return false;
                }""", pattern, role)
                if ok:
                    time.sleep(0.35)
                    self._trace_add("dom_click_scan", {"pattern": pattern, "ok": True})
                    return True

        self._trace_add("dom_click_failed", {"pattern": pattern})
        return False

    def click_button_by_markup(self, markup: List[ClickMarkup]) -> bool:
        if not markup:
            return False

        for mk in markup:
            if getattr(mk, "img_path", None) and hasattr(self, "_verify_and_click_by_image"):
                try:
                    if self._verify_and_click_by_image(mk):
                        self._trace_add("click_by_markup_image", {"name": mk.name, "img": mk.img_path, "ok": True})
                        time.sleep(0.25)
                        return True
                except Exception as e:
                    self._trace_add("click_by_markup_image_err", {"name": mk.name, "err": str(e)})

            try:
                if self._dom_click_by_markup(mk):
                    self._trace_add("click_by_markup_dom", {"name": mk.name, "ok": True})
                    time.sleep(0.25)
                    return True
            except Exception as e:
                self._trace_add("click_by_markup_dom_err", {"name": mk.name, "err": str(e)})

        self._trace_add("click_by_markup_none_matched", {"count": len(markup)})
        return False

    # ---------------------- Bildabgleich ----------------------
    def _clip_box(self, box: Tuple[int,int,int,int]) -> Tuple[int,int,int,int]:
        x1,y1,x2,y2 = box
        v = self.viewport_info()
        x1 = max(0, min(int(x1), int(v["iw"])))
        y1 = max(0, min(int(y1), int(v["ih"])))
        x2 = max(0, min(int(x2), int(v["iw"])))
        y2 = max(0, min(int(y2), int(v["ih"])))
        if x2 < x1: x1,x2 = x2,x1
        if y2 < y1: y1,y2 = y2,y1
        return (x1,y1,x2,y2)

    def _box_rel_to_css(self, rbox: Tuple[float,float,float,float]) -> Tuple[int,int,int,int]:
        v = self.viewport_info()
        x1 = int(round(rbox[0] * v["iw"]))
        y1 = int(round(rbox[1] * v["ih"]))
        x2 = int(round(rbox[2] * v["iw"]))
        y2 = int(round(rbox[3] * v["ih"]))
        return self._clip_box((x1,y1,x2,y2))

    def _image_find_in_region(self, ref_img: Path, box_css: Optional[Tuple[int,int,int,int]], min_score: float) -> Optional[Tuple[int,int,float]]:
        if cv2 is None or np is None:
            self._trace_add("opencv_absent", {"img": str(ref_img)}); return None

        shot = self._screenshot("img_gate")
        if not shot or not Path(shot).exists():
            return None

        img = cv2.imread(str(shot), cv2.IMREAD_COLOR)
        templ = cv2.imread(str(ref_img), cv2.IMREAD_COLOR)
        if img is None or templ is None:
            self._trace_add("opencv_read_failed", {"img": str(shot), "templ": str(ref_img)}); return None

        roi = img
        offset = (0,0)
        if box_css:
            x1,y1,x2,y2 = self._clip_box(box_css)
            roi = img[y1:y2, x1:x2]
            offset = (x1,y1)

        res = cv2.matchTemplate(roi, templ, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        self._trace_add("opencv_match", {"score": float(max_val), "loc": max_loc, "box": box_css})

        if float(max_val) < float(min_score):
            return None

        h, w = templ.shape[:2]
        cx = int(max_loc[0] + w/2) + offset[0]
        cy = int(max_loc[1] + h/2) + offset[1]

        if self.debug_on:
            try:
                cv2.rectangle(img, (cx - w//2, cy - h//2), (cx + w//2, cy + h//2), (0,255,0), 2)
                p = self.debug_dir / f"shot_{int(time.time()*1000)}_match_annot.png"
                cv2.imwrite(str(p), img)
                self._trace_add("match_annot_saved", {"path": str(p)})
            except Exception as e:
                self._trace_add("match_annot_err", {"error": str(e)})

        return (cx, cy, float(max_val))

    def _verify_and_click_by_image(self, mk: ClickMarkup) -> bool:
        if not mk.img_path:
            return False
        p = Path(mk.img_path)
        if not p.exists():
            self._trace_add("img_missing", {"path": mk.img_path})
            return False

        box_css = None
        if mk.expected_box_css:
            box_css = self._clip_box(mk.expected_box_css)
        elif mk.expected_box_rel:
            box_css = self._box_rel_to_css(mk.expected_box_rel)

        min_score = float(mk.min_score if mk.min_score is not None else self._image_match_threshold)
        found = self._image_find_in_region(p, box_css, min_score)
        if not found:
            return False

        cx, cy, score = found
        self._trace_add("img_gate_ok", {"cx": cx, "cy": cy, "score": score})
        return (self._pw_click(cx, cy) or self._pya_click(cx, cy))

    def _image_match_click(self, ref_img: Path) -> bool:
        if cv2 is None or np is None:
            self._trace_add("opencv_absent", {"img": str(ref_img)})
            return False
        try:
            shot = self._screenshot("image_match_base")
            if not shot or not Path(shot).exists():
                return False
            img = cv2.imread(str(shot), cv2.IMREAD_COLOR)
            templ = cv2.imread(str(ref_img), cv2.IMREAD_COLOR)
            if img is None or templ is None:
                self._trace_add("opencv_read_failed", {"img": str(shot), "templ": str(ref_img)})
                return False
            res = cv2.matchTemplate(img, templ, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            self._trace_add("opencv_match_full", {"score": float(max_val), "loc": max_loc})
            if float(max_val) >= self._image_match_threshold:
                h, w = templ.shape[:2]
                cx, cy = int(max_loc[0] + w/2), int(max_loc[1] + h/2)
                return (self._pw_click(cx, cy) or self._pya_click(cx, cy))
        except Exception as e:
            self._trace_add("opencv_error", {"error": str(e)})
        return False

    # --- NEU: simple CV-Flow (nur für Vollständigkeit; Web-Adapter hat bereits CV) ---
    def _cv_find_best(self, screenshot_path: Path, templ_img: Any, method=None) -> tuple[float, tuple[int,int]]:
        import cv2
        method = method or cv2.TM_CCOEFF_NORMED
        res = cv2.matchTemplate(screenshot_path, templ_img, method)  # type: ignore[arg-type]
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        return (float(max_val), (int(max_loc[0]), int(max_loc[1])))

    def _cv_find_template(self,
                          templ_path: Path,
                          *,
                          region_css: tuple[int,int,int,int] | None = None,
                          scales: list[float] = (0.85, 0.9, 0.95, 1.0, 1.05, 1.1),
                          threshold: float | None = None,
                          label: str = "cv_find") -> tuple[int,int,float] | None:
        if cv2 is None or np is None:
            self._trace_add("cv_absent", {"templ": str(templ_path)})
            return None

        shot = self._screenshot(label)
        if not shot or not shot.exists():
            return None

        img = cv2.imread(str(shot), cv2.IMREAD_COLOR)
        templ_orig = cv2.imread(str(templ_path), cv2.IMREAD_COLOR)
        if img is None or templ_orig is None:
            self._trace_add("cv_read_failed", {"shot": str(shot), "templ": str(templ_path)})
            return None

        roi = img
        offset = (0, 0)
        if region_css:
            x1, y1, x2, y2 = self._clip_box(region_css)
            roi = img[y1:y2, x1:x2]
            offset = (x1, y1)

        best = (-1.0, (0,0), (0,0))
        for s in scales:
            th = int(round(templ_orig.shape[0] * s))
            tw = int(round(templ_orig.shape[1] * s))
            if th < 8 or tw < 8:
                continue
            templ = cv2.resize(templ_orig, (tw, th), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(roi, templ, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if float(max_val) > best[0]:
                best = (float(max_val), (int(max_loc[0]), int(max_loc[1])), (tw, th))

        score, tl, (tw, th) = best
        if threshold is None:
            threshold = float(getattr(self.cfg, "image_match_threshold", 0.84))

        self._trace_add("cv_best", {"templ": str(templ_path), "score": score, "tl": tl, "size": (tw, th), "offset": offset})
        if score < float(threshold):
            return None

        cx = tl[0] + tw // 2 + offset[0]
        cy = tl[1] + th // 2 + offset[1]

        if self.debug_on:
            try:
                vis = img.copy()
                cv2.rectangle(vis,
                              (cx - tw//2, cy - th//2),
                              (cx + tw//2, cy + th//2),
                              (0, 255, 0), 2)
                out = self.debug_dir / f"shot_{int(time.time()*1000)}_{label}_annot.png"
                cv2.imwrite(str(out), vis)
                self._trace_add("cv_annot_saved", {"path": str(out)})
            except Exception as e:
                self._trace_add("cv_annot_err", {"error": str(e)})

        return (int(cx), int(cy), float(score))

    def enable_web_search_by_cv(self,
                                assets_dir: str | None = None,
                                *,
                                plus_name: str = "plus.jpg",
                                more_name: str = "more.jpg",
                                web_name: str  = "websearch.jpg",
                                threshold: float | None = None) -> bool:
        assets_dir = assets_dir or getattr(self.cfg, "assets_dir", "webbridge")
        plus_p = Path(assets_dir) / plus_name
        more_p = Path(assets_dir) / more_name
        web_p  = Path(assets_dir) / web_name

        with suppress(Exception):
            self.page.evaluate("""() => {
                const el = document.querySelector('[data-testid^="composer"]') || document.body;
                el.scrollIntoView({block:'end'});
            }""")

        v = self.viewport_info()
        iw, ih = int(v["iw"]), int(v["ih"])
        band_more = (0, int(ih * 0.58), iw, int(ih * 0.92))
        band_web  = (0, int(ih * 0.50), iw, int(ih * 0.95))

        thr = float(threshold if threshold is not None else getattr(self.cfg, "image_match_threshold", 0.84))

        # 1) PLUS
        self._trace_add("cv_plus_begin", {})
        hit = self._cv_find_template(plus_p, threshold=thr, label="find_plus")
        if not hit:
            self._trace_add("cv_plus_notfound", {"path": str(plus_p)})
            return False
        px, py, ps = hit
        self._trace_add("cv_plus_hit", {"x": px, "y": py, "score": ps})

        if not (self._pw_click(px, py) or self._pya_click(px, py)):
            self._trace_add("cv_plus_click_failed", {"x": px, "y": py})
            return False
        time.sleep(float(getattr(self.cfg, "mouse_after_plus_sleep", 0.18)))

        # 2) MORE (Hover)
        self._trace_add("cv_more_begin", {})
        mhit = self._cv_find_template(more_p, region_css=band_more, threshold=thr, label="find_more")
        if not mhit:
            mhit = self._cv_find_template(more_p, threshold=thr, label="find_more_full")
        if not mhit:
            self._trace_add("cv_more_notfound", {"path": str(more_p)})
            return False
        mx, my, ms = mhit
        self._trace_add("cv_more_hit", {"x": mx, "y": my, "score": ms})

        try:
            self.hover_force(mx, my,
                             backend=getattr(self.cfg, "hover_backend", "auto"),
                             enter_dx=int(getattr(self.cfg, "hover_enter_dx", -120)),
                             enter_dy=int(getattr(self.cfg, "hover_enter_dy", 0)),
                             wiggle_px=int(getattr(self.cfg, "hover_wiggle_px", 6)),
                             cycles=int(getattr(self.cfg, "hover_wiggle_cycles", 2)),
                             dwell=float(getattr(self.cfg, "hover_dwell_more", 0.35)))
        except Exception:
            self.hover(mx, my, backend=getattr(self.cfg, "hover_backend", "auto"), dwell=0.25, jiggle=True)

        time.sleep(0.18)

        # 3) WEB SEARCH
        self._trace_add("cv_web_begin", {})
        whit = self._cv_find_template(web_p, region_css=band_web, threshold=thr, label="find_web")
        if not whit:
            whit = self._cv_find_template(web_p, threshold=thr, label="find_web_full")
        if not whit:
            self._trace_add("cv_web_notfound", {"path": str(web_p)})
            return False

        wx, wy, ws = whit
        self._trace_add("cv_web_hit", {"x": wx, "y": wy, "score": ws})

        if self._pw_click(wx, wy) or self._pya_click(wx, wy):
            self._trace_add("cv_web_done", {"ok": True})
            return True

        self._trace_add("cv_web_click_failed", {"x": wx, "y": wy})
        return False

    # ---------------------- Window Mgmt ----------------------
    def try_maximize_window(self, title_hint: str = None) -> bool:
        if gw is None:
            self._trace_add("pygetwindow_absent", {})
            return False
        try:
            hint = (title_hint or self._title_hint or "Chrome").lower()
            titles = [t for t in gw.getAllTitles() if hint in (t or "").lower()]
            if not titles:
                self._trace_add("window_list_empty", {})
                return False
            for t in titles:
                with suppress(Exception):
                    win = gw.getWindowsWithTitle(t)[0]
                    if hasattr(win, "isMaximized") and not win.isMaximized:
                        win.maximize()
                        self._trace_add("window_maximized", {"title": t})
                        return True
            return False
        except Exception as e:
            self._trace_add("window_maximize_error", {"error": str(e)})
            return False

    # ---------------------- Punktauflösung ----------------------
    def resolve_point(self, name: str, mode: str,
                      abs_coords: Dict[str, Tuple[float,float]],
                      rel_coords: Dict[str, Tuple[float,float]]) -> Tuple[int,int]:
        v = self.viewport_info()
        iw, ih = int(v["iw"]), int(v["ih"])

        if mode == "rel" and name in rel_coords:
            return self.rel_to_css(rel_coords[name])
        if mode == "px" and name in abs_coords:
            x, y = abs_coords[name]; return int(round(x)), int(round(y))
        if mode == "screen" and name in abs_coords:
            off = (0,0)
            so = os.getenv("WB_SCREEN_OFFSET")
            if so and "," in so:
                with suppress(Exception):
                    ox, oy = so.split(",", 1); off = (int(ox), int(oy))
            return self.screen_to_css(abs_coords[name], offset=off)

        use_max = (str(getattr(self.cfg, "mouse_mode", "")).lower() == "maximized")

        def pick(base: str, base_max: str, default_xy: Tuple[int,int]) -> Tuple[int,int]:
            key = base_max if (use_max and hasattr(self.cfg, base_max)) else base
            return tuple(getattr(self.cfg, key, default_xy))

        use_temp = bool(getattr(self.cfg, "use_temporary_chat", False))

        composer = pick("mouse_composer_xy",     "mouse_composer_xy_max",     (int(iw*0.50), int(ih*0.92)))
        plus     = pick("mouse_plus_xy",         "mouse_plus_xy_max",         (int(iw*0.62), int(ih*0.76)))
        more     = pick("mouse_more_hover_xy",   "mouse_more_hover_xy_max",   (int(iw*0.72), int(ih*0.76)))

        if use_temp:
            web = pick("mouse_web_xy_tempchat",  "mouse_web_xy_tempchat",     (int(iw*0.75), int(ih*0.70)))
        else:
            web = pick("mouse_web_xy_regular",   "mouse_web_xy_regular_max",  (int(iw*0.85), int(ih*0.74)))

        mapping = {"composer": composer, "plus": plus, "more": more, "web": web}
        x, y = mapping.get(name, web)

        if hasattr(self, "_apply_point_offset"):
            x, y = self._apply_point_offset(name, int(x), int(y))

        self._trace_add("resolve_fallback", {"name": name, "css": (int(x), int(y)), "use_max": use_max})
        return int(x), int(y)

    def _apply_point_offset(self, name: str, x: int, y: int) -> Tuple[int,int]:
        cfg_attr = f"{name}_hover_offset" if name == "more" else f"{name}_offset"
        off = getattr(self.cfg, cfg_attr, None)
        if off and isinstance(off, (tuple, list)) and len(off) == 2:
            try:
                dx, dy = int(off[0]), int(off[1])
                x += dx; y += dy
            except Exception:
                pass
        env_key = f"WB_{name.upper()}_OFFSET"
        env_val = os.getenv(env_key)
        if env_val and "," in env_val:
            try:
                dx, dy = [int(s.strip()) for s in env_val.split(",", 1)]
                x += dx; y += dy
            except Exception:
                pass
        return int(x), int(y)

    # ---------------------- Public API: Click Pipeline ----------------------
    def click(self, req: ClickRequest, label: str = "") -> bool:
        self._trace.clear()
        self._trace_add("begin", {"label": label, "req": asdict(req)})

        for mk in (req.markup or []):
            if getattr(mk, "img_path", None):
                if self._verify_and_click_by_image(mk):
                    self._debug_dump(label or mk.name or "image")
                    return True

        if req.markup and self.click_button_by_markup(req.markup):
            self._debug_dump(label or "dom")
            return True

        target_css: Optional[Tuple[int,int]] = None
        if req.css:
            target_css = (int(req.css[0]), int(req.css[1]))
        elif req.rel:
            target_css = self.rel_to_css(req.rel)
        elif req.screen:
            off = (0,0)
            so = os.getenv("WB_SCREEN_OFFSET")
            if so and "," in so:
                with suppress(Exception):
                    ox, oy = so.split(",", 1)
                    off = (int(ox), int(oy))
            target_css = self.screen_to_css(req.screen, offset=off)

        if target_css:
            hit = self._peek_element_at(*target_css)
            if hit:
                self._trace_add("hit_test", hit)
            if self._pw_click(*target_css) or self._pya_click(*target_css):
                self._debug_dump(label or "coords")
                return True

        if req.allow_maximize and bool(getattr(self.cfg, "allow_window_maximize", True)):
            if self.try_maximize_window(self._title_hint):
                time.sleep(0.3)
                if req.markup and self.click_button_by_markup(req.markup):
                    self._debug_dump(label or "dom_after_max")
                    return True
                if target_css and (self._pw_click(*target_css) or self._pya_click(*target_css)):
                    self._debug_dump(label or "coords_after_max")
                    return True

        self._debug_dump(label or "failed")
        return False

    # Komfort: simpler Sequenz-Flow
    def enable_web_search(self, rel_map: Dict[str, Tuple[float,float]] = None,
                          abs_map_css: Dict[str, Tuple[int,int]] = None,
                          names_seq: Iterable[str] = ("composer","plus","more","web"),
                          markup: List[ClickMarkup] = None,
                          mode: str = "rel") -> bool:
        rel_map = rel_map or {}
        abs_map_css = abs_map_css or {}
        markup = markup or []
        ok = False

        if markup:
            for m in markup:
                if self._dom_click_by_markup(m):
                    ok = True
                    break

        if not ok:
            for name in names_seq:
                req = ClickRequest(
                    rel = rel_map.get(name) if mode == "rel" else None,
                    css = abs_map_css.get(name) if mode == "px" else None,
                    markup = [m for m in markup if (m.name == name or not m.name)]
                )
                if not self.click(req, label=f"seq_{name}"):
                    return False
        return True

    # Komfort: + → hover More → Web (mit Bild/DOM/Koordinate)
    def hover_menu_then_click(self,
                              *,
                              mode: str,
                              abs_coords: Dict[str, Tuple[float,float]],
                              rel_coords: Dict[str, Tuple[float,float]],
                              markup: List[ClickMarkup],
                              hover_backend: Optional[str] = None,
                              dwell_more: float = 0.35) -> bool:
        hb = (hover_backend
              or getattr(self, "_hover_backend", None)
              or getattr(self.cfg, "hover_backend", None)
              or os.getenv("WB_HOVER_BACKEND")
              or "auto")
        mdur = float(getattr(self.cfg, "mouse_move_duration", 0.12))

        with suppress(Exception):
            self.page.evaluate("""() => {
                const el = document.querySelector('[data-testid^="composer"]') || document.body;
                el.scrollIntoView({block:'end'});
            }""")

        def _by_name(name: str) -> List[ClickMarkup]:
            return [m for m in (markup or []) if (m.name == name)]

        def _click_by_markup_list(mks: List[ClickMarkup]) -> bool:
            if not mks: return False
            if hasattr(self, "click_button_by_markup"):
                return bool(self.click_button_by_markup(mks))
            for m in mks:
                if self._dom_click_by_markup(m):
                    return True
            return False

        web_mks = [m for m in (markup or []) if (m.name == "web" or m.name == "")]
        def _web_visible() -> bool:
            if hasattr(self, "_is_markup_visible"):
                try:
                    return any(self._is_markup_visible(mk) for mk in web_mks)
                except Exception:
                    pass
            try:
                for mk in web_mks:
                    if mk.text:
                        loc = self.page.get_by_role(mk.role or "button", name=re.compile(mk.text, re.I))
                        if loc.count() and loc.first.is_visible():
                            return True
                    for sel in mk.selectors:
                        loc = self.page.locator(sel)
                        if loc.count() and loc.first.is_visible():
                            return True
            except Exception:
                pass
            return False

        def _hover_active(x: int, y: int, backend: str, attempt: int = 0):
            enter_dx = int(getattr(self.cfg, "hover_enter_dx", -120))
            enter_dy = int(getattr(self.cfg, "hover_enter_dy", 0))
            wiggle_px = int(getattr(self.cfg, "hover_wiggle_px", 6)) + attempt*2
            cycles = int(getattr(self.cfg, "hover_wiggle_cycles", 2)) + (1 if attempt else 0)
            dwell = float(getattr(self.cfg, "hover_dwell_more", dwell_more)) + 0.05*attempt

            if hasattr(self, "hover_force"):
                return self.hover_force(x, y,
                                        backend=backend,
                                        enter_dx=enter_dx, enter_dy=enter_dy,
                                        wiggle_px=wiggle_px, cycles=cycles,
                                        dwell=dwell)

            with suppress(Exception):
                self.move_mouse(x + enter_dx, y + enter_dy, backend=backend, duration=max(0.08, mdur))
            self.move_mouse(x, y, backend=backend, duration=max(0.10, mdur))
            for _ in range(max(1, cycles)):
                with suppress(Exception):
                    self.move_mouse(x + wiggle_px, y, backend=backend, duration=0.04)
                    self.move_mouse(x - wiggle_px, y, backend=backend, duration=0.04)
                    self.move_mouse(x, y, backend=backend, duration=0.03)
            time.sleep(max(0.05, dwell))
            self._trace_add("hover_force_inline", {
                "x": x, "y": y, "backend": backend,
                "enter_dx": enter_dx, "enter_dy": enter_dy,
                "wiggle_px": wiggle_px, "cycles": cycles, "dwell": dwell
            })
            return True

        # ===== 1) PLUS =====
        plus_mks = _by_name("plus")
        did_plus = False
        for m in plus_mks:
            if getattr(m, "img_path", None) and hasattr(self, "_verify_and_click_by_image"):
                if self._verify_and_click_by_image(m):
                    time.sleep(0.20)
                    self._trace_add("plus_via_image", {"ok": True}); did_plus = True
                    break
        if not did_plus and plus_mks:
            if _click_by_markup_list(plus_mks):
                time.sleep(0.20)
                self._trace_add("plus_via_dom", {"ok": True}); did_plus = True
        if not did_plus:
            px, py = self.resolve_point("plus", mode, abs_coords, rel_coords)
            if not (self._pw_click(px, py) or self._pya_click(px, py)):
                self._trace_add("plus_click_failed", {"x": px, "y": py})
                return False

        # ===== 2) MORE =====
        mx, my = self.resolve_point("more", mode, abs_coords, rel_coords)
        self._trace_add("hover_more_begin", {"x": mx, "y": my, "backend": hb})

        open_retries = int(getattr(self.cfg, "hover_open_retries", 3))
        opened = False
        backend = hb

        for attempt in range(max(1, open_retries)):
            _hover_active(mx, my, backend=backend, attempt=attempt)

            visible = False
            for _ in range(4):
                if _web_visible():
                    visible = True
                    break
                time.sleep(0.08)

            if visible:
                opened = True
                break

            if attempt == 0:
                alt = "playwright" if (backend in ("os", "auto")) else "os"
                self._trace_add("hover_backend_switch", {"from": backend, "to": alt})
                backend = alt

        self._trace_add("hover_more_done", {"x": mx, "y": my, "opened": opened})

        # ===== 3) WEB =====
        for m in web_mks:
            if getattr(m, "img_path", None) and hasattr(self, "_verify_and_click_by_image"):
                if self._verify_and_click_by_image(m):
                    time.sleep(0.25)
                    self._trace_add("web_via_image", {"ok": True})
                    return True

        if web_mks and _click_by_markup_list(web_mks):
            time.sleep(0.25)
            self._trace_add("web_via_dom", {"ok": True})
            return True

        wx, wy = self.resolve_point("web", mode, abs_coords, rel_coords)
        if self._pw_click(wx, wy) or self._pya_click(wx, wy):
            self._trace_add("web_via_coords", {"x": wx, "y": wy, "ok": True})
            return True

        self._trace_add("web_click_failed", {})
        return False

    # --- Minimaler deterministischer Koordinaten-Flow ---
    def hover_plus_more_then_click_web_coords_only(self,
                                                   *,
                                                   mode: str,
                                                   abs_coords: Dict[str, Tuple[float,float]],
                                                   rel_coords: Dict[str, Tuple[float,float]],
                                                   hover_backend: str = "auto") -> bool:
        def _get_xy(name: str) -> Tuple[int,int]:
            if mode == "px":
                return tuple(map(int, abs_coords.get(name) or (0,0)))
            elif mode == "rel":
                rx, ry = rel_coords.get(name) or (0.0, 0.0)
                return self.rel_to_css((rx, ry))
            else:
                return tuple(map(int, abs_coords.get(name) or (0,0)))

        def _click_css(x: int, y: int) -> bool:
            return (self._pw_click(x, y) or self._pya_click(x, y))

        def _vibrate(x: int, y: int, *, radius: int = 3, steps: int = 4, dwell: float = 0.12):
            self.move_mouse(x, y, backend=hover_backend, duration=max(0.08, float(getattr(self.cfg, "mouse_move_duration", 0.10))))
            for i in range(max(1, steps)):
                with suppress(Exception):
                    self.move_mouse(x + (radius if i % 2 == 0 else -radius), y, backend=hover_backend, duration=0.04)
                    self.move_mouse(x, y, backend=hover_backend, duration=0.04)
            time.sleep(dwell)

        px, py = _get_xy("plus")
        self._trace_add("step_plus", {"x": px, "y": py})
        if not _click_css(px, py):
            self._trace_add("plus_failed", {"x": px, "y": py})
            return False
        time.sleep(float(getattr(self.cfg, "menu_open_settle", getattr(self.cfg, "mouse_after_plus_sleep", 0.18))))

        mx, my = _get_xy("more")
        self._trace_add("step_more_hover", {"x": mx, "y": my, "backend": hover_backend})
        _vibrate(mx, my,
                 radius=int(getattr(self.cfg, "hover_jitter_radius", 3)),
                 steps=int(getattr(self.cfg, "hover_jitter_steps", 4)),
                 dwell=float(getattr(self.cfg, "hover_settle_after_open", getattr(self.cfg, "hover_dwell_more", 0.18))))
        for dx, dy in getattr(self.cfg, "more_rehover_offsets", [(0,0),(8,0),(-8,0),(0,8),(0,-8)]):
            _vibrate(mx+dx, my+dy, radius=2, steps=2, dwell=0.08)

        wx, wy = _get_xy("web")
        self._trace_add("step_web_click_exact", {"x": wx, "y": wy})
        if _click_css(wx, wy):
            return True

        for ox, oy in [(2,0), (-2,0), (0,2), (0,-2), (3,1), (-3,-1)]:
            if _click_css(wx+ox, wy+oy):
                self._trace_add("web_click_nudge_ok", {"x": wx+ox, "y": wy+oy})
                return True

        self._trace_add("web_click_failed", {"x": wx, "y": wy})
        return False
