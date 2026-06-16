# webbridge/coords.py
from __future__ import annotations
from typing import Dict, Tuple, Any
import os

def _parse_abs_env(env: str) -> Dict[str, Tuple[int, int]]:
    out = {}
    for part in (env or "").split(";"):
        part = part.strip()
        if not part: continue
        k, _, v = part.partition("=")
        xs = v.split(",")
        if len(xs) != 2: continue
        try:
            x, y = int(xs[0]), int(xs[1])
            key = k.strip().lower()
            mp = {"composer":"composer_xy","plus":"plus_xy","more":"more_xy","web":"web_xy"}
            out[mp.get(key, key)] = (x, y)
        except Exception:
            pass
    return out

def build_abs_coords(cfg, abs_ui: dict) -> Dict[str, Tuple[int, int]]:
    """
    Baut absolute CSS-/Screen-Pixel-Koordinaten aus prompt.json["ui"]["absolute"].
    Akzeptiert Keys: "composer", "plus", "more", "web" (oder jeweils *_xy).
    Fällt ansonsten auf cfg-Defaults zurück.
    """
    def _pair(key, default):
        v = abs_ui.get(key) or abs_ui.get(f"{key}_xy")
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return (int(v[0]), int(v[1]))
        return tuple(default)

    return {
        "composer": _pair("composer", cfg.mouse_composer_xy),
        "plus":     _pair("plus",     cfg.mouse_plus_xy),
        "more":     _pair("more",     cfg.mouse_more_hover_xy),
        "web":      _pair(
                        "web",
                        cfg.mouse_web_xy_tempchat if getattr(cfg, "use_temporary_chat", False)
                        else cfg.mouse_web_xy_regular
                     ),
    }


def build_rel_coords(rel_ui: dict) -> Dict[str, Tuple[float, float]]:
    """
    Baut relative Koordinaten (0..1) aus prompt.json["ui"]["relative"].
    Akzeptiert Keys: "composer", "plus", "more", "web" (oder *_rel).
    Nur gültige Paare werden ausgegeben (sonst Key fehlt).
    """
    def _pair(key):
        v = rel_ui.get(key) or rel_ui.get(f"{key}_rel")
        if isinstance(v, (list, tuple)) and len(v) == 2:
            try:
                x = float(v[0]); y = float(v[1])
                # clamp 0..1
                x = 0.0 if x < 0 else 1.0 if x > 1 else x
                y = 0.0 if y < 0 else 1.0 if y > 1 else y
                return (x, y)
            except Exception:
                return None
        return None

    out: Dict[str, Tuple[float, float]] = {}
    for k in ("composer", "plus", "more", "web"):
        p = _pair(k)
        if p is not None:
            out[k] = p
    return out