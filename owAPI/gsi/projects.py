from __future__ import annotations
import os, json, logging
from typing import Dict

LOG = logging.getLogger("gpt_sheet")
PROJECTS_FILE = os.path.join(os.getcwd(), "projects.json")

def _load() -> Dict[str, Dict[str, str]]:
    if not os.path.isfile(PROJECTS_FILE):
        return {}
    try:
        with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        LOG.error(f"Konnte projects.json nicht lesen: {exc}")
        return {}

def _save(db: Dict[str, Dict[str, str]]) -> None:
    try:
        with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        LOG.error(f"Konnte projects.json nicht schreiben: {exc}")

def load_projects() -> Dict[str, Dict[str, str]]:
    return _load()

def create_project(name: str, url: str = "", desc: str = "") -> None:
    db = _load()
    if name in db:
        raise ValueError(f"Projekt existiert bereits: {name}")
    db[name] = {"url": url, "description": desc}
    _save(db)

def set_url(name: str, url: str) -> None:
    db = _load()
    if name not in db:
        raise ValueError(f"Projekt nicht gefunden: {name}")
    db[name]["url"] = url
    _save(db)

def set_desc(name: str, desc: str) -> None:
    db = _load()
    if name not in db:
        raise ValueError(f"Projekt nicht gefunden: {name}")
    db[name]["description"] = desc
    _save(db)

def delete_project(name: str) -> None:
    db = _load()
    if name not in db:
        raise ValueError(f"Projekt nicht gefunden: {name}")
    del db[name]
    _save(db)

def rename_project(old: str, new: str) -> None:
    db = _load()
    if old not in db:
        raise ValueError(f"Projekt nicht gefunden: {old}")
    if new in db:
        raise ValueError(f"Projekt existiert bereits: {new}")
    db[new] = db.pop(old)
    _save(db)
