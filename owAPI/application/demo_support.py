# ersetzen in File application/demo_browser.py
from __future__ import annotations

from pathlib import Path
import argparse
import sys

# --- Ensure project ROOT on sys.path (NOT the 'webbridge' folder itself) ---
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Sanity check
if not (ROOT / "webbridge" / "__init__.py").exists():
    raise SystemExit("[fatal] package 'webbridge' not found at expected location: "
                     f"{ROOT / 'webbridge']}")

# Absolute package imports
from webbridge.ki_layer import OADS2KI
from webbridge.config import BridgeConfig

# Demo helpers (umbenannt)
from application.application_support import (
    load_project,
    build_initial_prompt,
    start_conversation_simple,
    continue_simple,
    end_conversation_simple,
    startover_if_supported,
    debug_out,
    diag_loaded,
)


def _parse_args() -> argparse.Namespace:
    # Funktion _parse_args – ersetzen in File application/demo_browser.py / Funktion _parse_args kann entfernt werden
    ap = argparse.ArgumentParser()
    ap.add_argument("--projekt", "--project", dest="project", required=True)
    ap.add_argument("--no-websearch", action="store_true", help="Websuche deaktivieren")
    return ap.parse_args()


def run_demo() -> int:
    # Funktion run_demo – ersetzen in File application/demo_browser.py / Funktion run_demo kann entfernt werden
    args = _parse_args()
    proj = load_project(ROOT, args.project)
    initial_prompt = build_initial_prompt(ROOT, proj)

    cfg = BridgeConfig()
    with OADS2KI(cfg, verbose=True) as ki:
        diag_loaded(OADS2KI)

        # --- Konversation 1 ---
        r1 = start_conversation_simple(ki, initial_prompt, websearch=not args.no_websearch)
        debug_out("START#1", r1)

        if getattr(r1, "ok", False):
            r2 = continue_simple(ki, "test-weiter-1")
            debug_out("CONT#1a", r2)

            r3 = continue_simple(ki, "test-weiter-2")
            debug_out("CONT#1b", r3)

            end_conversation_simple(ki)

        # --- Konversation 2 ---
        r4 = start_conversation_simple(ki, initial_prompt, websearch=not args.no_websearch)
        debug_out("START#2", r4)

        if getattr(r4, "ok", False):
            r5 = continue_simple(ki, "test-weiter-3")
            debug_out("CONT#2a", r5)
            end_conversation_simple(ki)

        # --- Browser-Startover (hart: Browser schließen & neu öffnen) ---
        ok = startover_if_supported(ki)
        if not ok:
            print("[warn] Startover hat nicht gegriffen.", flush=True)

        # --- Konversation 3 ---
        r6 = start_conversation_simple(ki, initial_prompt, websearch=not args.no_websearch)
        debug_out("START#3", r6)
        if getattr(r6, "ok", False):
            end_conversation_simple(ki)

    return 0


def main():
    # Funktion main – ersetzen in File application/demo_browser.py
