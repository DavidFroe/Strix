# bei einfügen in File application/application_support.py
from __future__ import annotations
from pathlib import Path
import json
from typing import Any

# Funktion load_project – bei einfügen in File application/application_support.py / Funktion load_project kann entfernt werden
def load_project(ROOT: Path, project_name: str) -> dict:
    pj = ROOT / "projects.json"
    data = json.loads(pj.read_text(encoding="utf-8"))

    if isinstance(data, dict) and project_name in data and isinstance(data[project_name], dict):
        d = dict(data[project_name]); d["name"] = project_name; return d

    if isinstance(data, dict) and isinstance(data.get("projects"), list):
        for p in data["projects"]:
            if isinstance(p, dict) and p.get("name") == project_name:
                return p

    if isinstance(data, list):
        for p in data:
            if isinstance(p, dict) and p.get("name") == project_name:
                return p

    raise KeyError(f"Projekt '{project_name}' nicht gefunden oder falsches Format in projects.json.")

# Funktion build_initial_prompt – bei einfügen in File application/application_support.py / Funktion build_initial_prompt kann entfernt werden
def build_initial_prompt(ROOT: Path, proj: dict) -> str:
    role_text = (ROOT / "prompt_system_owapi_de.txt").read_text(encoding="utf-8", errors="ignore")
    desc = proj.get("description", ""); url = proj.get("url", "")
    return (f"{role_text}\n\n[Projekt]: {proj['name']}\n[SheetURL]: {url}\n"
            f"[Hinweis]: status.json ist zu Beginn leer.\n\n[AUFGABE]:\n{desc}\n")

# Funktion start_conversation_simple – bei einfügen in File application/application_support.py / Funktion start_conversation_simple kann entfernt werden
def start_conversation_simple(ki: Any, initial_prompt: str, *, websearch: bool) -> Any:
    return ki.start_conversation(role="", prompt=initial_prompt, websearch=websearch, temp_chat=False)

# Funktion continue_simple – bei einfügen in File application/application_support.py / Funktion continue_simple kann entfernt werden
def continue_simple(ki: Any, message: str = "test-weiter") -> Any:
    return ki.continue_conversation(json.dumps({"decision": "finish", "message": message}, ensure_ascii=False))

# Funktion end_conversation_simple – bei einfügen in File application/application_support.py / Funktion end_conversation_simple kann entfernt werden
def end_conversation_simple(ki: Any) -> None:
    try: ki.end_conversation()
    except Exception as e: print(f"[warn] end_conversation fehlgeschlagen: {e}", flush=True)

# Funktion startover_if_supported – bei einfügen in File application/application_support.py / Funktion startover_if_supported kann entfernt werden
def startover_if_supported(ki: Any) -> bool:
    for name in ("startover", "restart_browser", "driver_restart"):
        fn = getattr(ki, name, None)
        if callable(fn):
            try:
                ok = bool(fn())
                print(f"[diag] Browser-Startover via ki.{name}() → {ok}", flush=True)
                return ok
            except Exception as e:
                print(f"[warn] ki.{name}() scheiterte: {e}", flush=True)
                return False
    print("[warn] Startover nicht verfügbar (keine passende Methode gefunden).", flush=True)
    return False

# Funktion debug_out – bei einfügen in File application/application_support.py / Funktion debug_out kann entfernt werden
def debug_out(tag: str, r: Any) -> None:
    import json as _json
    ok = getattr(r, "ok", False); err = getattr(r, "error", "unknown")
    print(f"{tag} :", "OK" if ok else f"FAIL → {err}", flush=True)
    data = getattr(r, "data", None)
    if data is not None: print(_json.dumps(data, ensure_ascii=False, indent=2))
    timings = getattr(r, "timings", None)
    if timings is not None: print(f"TIMINGS({tag}):", _json.dumps(timings, ensure_ascii=False, indent=2))

# Funktion diag_loaded – bei einfügen in File application/application_support.py / Funktion diag_loaded kann entfernt werden
def diag_loaded(class_obj: Any) -> None:
    import inspect
    try: print(f"[diag] OADS2KI loaded from: {inspect.getfile(class_obj)}", flush=True)
    except Exception: pass
