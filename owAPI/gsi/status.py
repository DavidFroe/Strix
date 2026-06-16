# gsi/status.py
from __future__ import annotations
from typing import Any, Dict, List, Optional

def _compact_diagnostics(diags: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for d in diags:
        out.append({
            "level": d.get("level"),
            "code": d.get("code"),
            "message": d.get("message"),
        })
    return out

def _compact_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in results:
        item = {
            "action": r.get("action"),
            "status": r.get("status"),
        }
        if "row" in r and r["row"] is not None:
            item["row"] = r["row"]
        if "error" in r and r["error"]:
            item["error"] = r["error"]
        if "meta" in r and r["meta"]:
            item["meta"] = r["meta"]
        out.append(item)
    return out

def _tabs_overview(tabs: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not isinstance(tabs, list):
        return []
    out = []
    for t in tabs:
        if not isinstance(t, dict):
            # falls jemand versehentlich Strings/sonstiges liefert: ignorieren
            continue
        out.append({
            "title": t.get("title"),
            "sheetId": t.get("sheetId"),
            "index": t.get("index"),
            "rows": t.get("rows"),
            "cols": t.get("cols"),
            "headers": t.get("headers") or [],
        })
    return out

def _should_compact_success(results: List[Dict[str, Any]], diagnostics: List[Dict[str, Any]]) -> bool:
    if any(d.get("level") in {"warning", "error"} for d in diagnostics):
        return False
    if any(r.get("status") != "success" for r in results):
        return False
    return True

def _normalize_flags(flags: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = {
        "allow_auto_tab_create": False,
        "allow_auto_headers": True,
        "include_snapshots": True,
        "autocreate_project": False,
    }
    if isinstance(flags, dict):
        for k in list(base.keys()):
            if k in flags:
                base[k] = bool(flags[k])
    return base

def _coerce_flags_tabs(
    flags: Optional[Any],
    tabs: Optional[Any],
) -> tuple[Optional[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
    """
    Akzeptiert beide Aufrufvarianten:
      build_guidance(..., flags, tabs, ...)
      build_guidance(..., tabs, flags, ...)
    und korrigiert, falls vertauscht.
    """
    # korrekt? flags=dict oder None, tabs=list|None
    if (flags is None or isinstance(flags, dict)) and (tabs is None or isinstance(tabs, list)):
        return flags, tabs

    # Vertauscht? flags ist list (eigentlich tabs), tabs ist dict (eigentlich flags)
    if isinstance(flags, list) and (tabs is None or isinstance(tabs, dict)):
        return tabs if isinstance(tabs, dict) else None, flags  # -> (flags, tabs)

    # flags ist dict aber tabs ist dict (falsch): nimm flags als flags, tabs leer
    if isinstance(flags, dict) and isinstance(tabs, dict):
        return flags, []

    # flags ist list, tabs ist list: nimm erste als tabs, flags leer
    if isinstance(flags, list) and isinstance(tabs, list):
        return None, flags

    # Fallback: nichts zu retten
    return None, []

def build_guidance(
    project: Optional[str],
    sheet_url: Optional[str],
    worksheet: Optional[str],
    headers: Optional[List[str]],
    diagnostics: List[Dict[str, Any]],
    sheet_events: List[Dict[str, Any]],
    tabs: Optional[List[Dict[str, Any]]] = None,
    flags: Optional[Dict[str, Any]] = None,
    service_account_email: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Liefert ein reichhaltiges Guidance-Objekt für status.json.
    Enthält Quickstart, alle Action-Typen inkl. 'append_rows' & '*_where',
    sowie einen Prompt-Text für ChatGPT.
    """
    tabs = tabs or []
    flags = flags or {}

    # Kurze Projekt-/Sheet-Zusammenfassung
    overview = {
        "project": project,
        "sheet_url": sheet_url,
        "worksheet": worksheet,
        "headers_detected": headers or [],
        "service_account_email": service_account_email,
        "tabs": tabs,
        "flags": flags,
    }

    # Quickstart
    quickstart = {
        "what_this_is": "Schnittstelle zwischen ChatGPT und Google Sheets.",
        "how_to_view_sheet": "Öffne die Google-Tabelle über 'sheet_url' in diesem Guidance-Block.",
        "quick_new_file": {
            "description": "Neues Projekt bequem anlegen, indem update.json nicht einmal JSON sein muss.",
            "format": "Erstes Wort MUSS 'neu' sein (Groß/Kleinschreibung egal), danach die Sheet-URL und ein *einzelnes* Wort als Projektname.",
            "example": "neu  https://docs.google.com/spreadsheets/d/XXXX/edit?gid=0  Stellen_Kontakte"
        },
        "json_shortcut": {
            "description": "Alternativ per valider JSON:",
            "example": {
                "new": True,
                "project": "Stellen_Kontakte",
                "url": "https://docs.google.com/spreadsheets/d/XXXX/edit?gid=0"
            }
        }
    }

    # API & Beispiele
    api = {
        "row_actions": [
            {
                "type": "append",
                "desc": "Eine einzelne Zeile anhängen.",
                "example": {
                    "project": project,
                    "sheet": {"url": sheet_url, "worksheet": worksheet or "Kontakte"},
                    "ops": [
                        {
                            "op": "append",
                            "worksheet": worksheet or "Kontakte",
                            "values": { h: f"<{h}-Wert>" for h in (headers or ["Datum","Firma","Standort"]) }
                        }
                    ]
                }
            },
            {
                "type": "append_rows",
                "desc": "Mehrere Zeilen auf einmal anhängen (Batch).",
                "example": {
                    "project": project,
                    "sheet": {"url": sheet_url, "worksheet": worksheet or "Kontakte"},
                    "ops": [
                        {
                            "op": "append_rows",
                            "worksheet": worksheet or "Kontakte",
                            "rows": [
                                { h: f"<{h}-Wert 1>" for h in (headers or ["Datum","Firma","Standort"]) },
                                { h: f"<{h}-Wert 2>" for h in (headers or ["Datum","Firma","Standort"]) }
                            ]
                        }
                    ]
                }
            },
            {
                "type": "edit",
                "desc": "Zellen in einer konkreten Zeile ändern.",
                "example": {
                    "ops": [
                        {"op": "edit", "worksheet": worksheet or "Kontakte", "row": 5,
                         "values": { (headers or ["Status"])[0]: "Kontaktaufnahme geplant" }}
                    ]
                }
            },
            {
                "type": "delete",
                "desc": "Eine Zeile entfernen.",
                "example": {
                    "ops": [
                        {"op": "delete", "worksheet": worksheet or "Kontakte", "row": 5}
                    ]
                }
            },
            {
                "type": "comment",
                "desc": "Wie 'edit', nur semantisch als Kommentar.",
                "example": {
                    "ops": [
                        {"op": "comment", "worksheet": worksheet or "Kontakte", "row": 6,
                         "values": { (headers or ["Notiz"])[0]: "per Telefon erreicht" }}
                    ]
                }
            },
        ],
        "bulk_where_actions": [
            {
                "type": "find_rows",
                "desc": "Zeilen anhand einer WHERE-DSL finden (and/or, eq, contains, icontains, regex).",
                "where_example": {"and": [{"col": "Status", "op": "icontains", "value": "geplant"},
                                          {"col": "Kanal", "op": "eq", "value": "Web/Portal"}]},
                "example": {
                    "ops": [
                        {"op": "find_rows", "worksheet": worksheet or "Kontakte",
                         "where": {"and": [{"col":"Status","op":"icontains","value":"geplant"}]}}
                    ]
                }
            },
            {
                "type": "edit_where",
                "desc": "Mehrere Zeilen per WHERE-Kriterien ändern.",
                "example": {
                    "ops": [
                        {"op": "edit_where", "worksheet": worksheet or "Kontakte",
                         "where": {"and": [{"col":"Firma","op":"icontains","value":"Rheinmetall"}]},
                         "values": {"Status": "Kontaktaufnahme geplant"}}
                    ]
                }
            },
            {
                "type": "delete_where",
                "desc": "Mehrere Zeilen per WHERE-Kriterien löschen (von unten nach oben).",
                "example": {
                    "ops": [
                        {"op": "delete_where", "worksheet": worksheet or "Kontakte",
                         "where": {"or": [{"col":"Status","op":"eq","value":"Test-Datensatz"}]}}
                    ]
                }
            },
        ],
        "tab_actions": [
            {"type": "create_tab", "desc": "Neuen Tab anlegen und optional Header setzen."},
            {"type": "rename_tab", "desc": "Tab umbenennen."},
            {"type": "delete_tab", "desc": "Tab löschen."},
            {"type": "set_headers", "desc": "Headerzeile setzen/überschreiben."},
            {"type": "list_tabs", "desc": "Tabs werden im Status/Gudance aufgeführt."},
        ],
        "headers_auto": "Wenn allow_auto_headers=true und noch keine Header vorhanden sind, leiten wir sie aus den Keys der ersten append/append_rows-Operation ab.",
        "how_to_request": "Setze in update.json 'request_guidance': true (oder 'help': true), um diese Guidance gezielt zu bekommen."
    }

    # Prompt-Vorschlag für ChatGPT (dein 'gopt')
    prompt_for_chatgpt = (
        "Die Ausgaben dieser Problemstellung sollen in einer Google-Tabelle gespeichert werden. "
        f"Du kannst die Tabelle unter '{sheet_url or '<Sheet-URL fehlt>'}' einsehen. "
        "Du darfst sie mit Hilfe der owAPI (update.json) bearbeiten. "
        "Wenn du Datensätze anhängen willst, sende eine update.json mit 'ops' und den passenden Spaltennamen. "
        "Nutze bei vielen Zeilen 'append_rows'. Für Massenänderungen nutze 'edit_where' bzw. 'delete_where' mit WHERE-Bedingungen. "
        "Falls die Tabelle noch keine Header hat, werden sie bei allow_auto_headers=true automatisch aus deinen Daten abgeleitet."
    )

    return {
        "overview": overview,
        "quickstart": quickstart,
        "api": api,
        "prompt_for_chatgpt": prompt_for_chatgpt,
        "diagnostics": diagnostics,
        "sheet_events": sheet_events,
    }

def construct_status(
    project: Optional[str],
    update_id: Optional[str],
    sheet_url: Optional[str],
    results: List[Dict[str, Any]],
    diagnostics: List[Dict[str, Any]],
    guidance: Optional[Dict[str, Any]],
    tabs: List[Dict[str, Any]],
    sheet_events: List[Dict[str, Any]],
    spreadsheet_title: Optional[str],
    context: Optional[Dict[str, Any]],
    include_guidance: Optional[bool] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    compact = _should_compact_success(results, diagnostics)

    status: Dict[str, Any] = {
        "project": project,
        "update_id": update_id,
        "sheet_url": sheet_url,
        "spreadsheet": {
            "id": None,
            "title": spreadsheet_title
        },
        "results": _compact_results(results) if results else [],
        "diagnostics": _compact_diagnostics(diagnostics),
        "sheet_events": sheet_events or [],
        "tabs": _tabs_overview(tabs),
        "context": context or {},
        "version": "1.9",
    }

    if sheet_url and "/d/" in sheet_url:
        try:
            sid = sheet_url.split("/d/")[1].split("/")[0]
            status["spreadsheet"]["id"] = sid
        except Exception:
            pass

    if guidance:
        if include_guidance is True:
            status["guidance"] = guidance
        elif include_guidance is False:
            status["guidance_hint"] = {
                "available": True,
                "how_to_request": "Sende in update.json zusätzlich \"request_guidance\": true (oder setze \"help\": true)."
            }
        else:
            if compact:
                status["guidance_hint"] = {
                    "available": True,
                    "how_to_request": "Sende in update.json zusätzlich \"request_guidance\": true (oder setze \"help\": true)."
                }
            else:
                status["guidance"] = guidance

    if compact:
        status["server_notes"] = "Alle Aktionen erfolgreich verarbeitet."
    else:
        if any(d.get("level") == "error" for d in diagnostics):
            status["server_notes"] = "Ein oder mehrere Fehler sind aufgetreten."
        else:
            status["server_notes"] = "Vorgang abgeschlossen (mit Hinweisen)."

    return status
