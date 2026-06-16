# ersetzen in File application/demo_browser.py
from __future__ import annotations
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if not (ROOT / "webbridge" / "__init__.py").exists():
    raise SystemExit("[fatal] package 'webbridge' not found at expected location: "
                     f"{ROOT / 'webbridge'}")

from webbridge.ki_layer import OADS2KI
from webbridge.config import BridgeConfig

# geänderter Import (umbenannt): application_support
from application.application_support import (
    load_project, build_initial_prompt, start_conversation_simple,
    continue_simple, end_conversation_simple, startover_if_supported,
    debug_out, diag_loaded,
)

# Funktion _parse_args – ersetzen in File application/demo_browser.py / Funktion _parse_args kann entfernt werden
def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--projekt", "--project", dest="project", required=True)
    ap.add_argument("--no-websearch", action="store_true", help="Websuche deaktivieren")
    return ap.parse_args()

# Funktion run_demo – ersetzen in File application/demo_browser.py / Funktion run_demo kann entfernt werden
def run_demo() -> int:
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
            debug_out("CONT#1a", continue_simple(ki, "test-weiter-1"))
            debug_out("CONT#1b", continue_simple(ki, "test-weiter-2"))
            end_conversation_simple(ki)

        # --- Konversation 2 ---
        debug_out("START#2", start_conversation_simple(ki, initial_prompt, websearch=not args.no_websearch))
        debug_out("CONT#2a", continue_simple(ki, "test-weiter-3"))
        end_conversation_simple(ki)

        # --- Harter Browser-Neustart (Startover) ---
        startover_if_supported(ki)

        # --- Konversation 3 ---
        debug_out("START#3", start_conversation_simple(ki, initial_prompt, websearch=not args.no_websearch))
        end_conversation_simple(ki)

    return 0

# Funktion main – ersetzen in File application/demo_browser.py / Funktion main kann entfernt werden
def main():
    raise SystemExit(run_demo())

if __name__ == "__main__":
    main()
