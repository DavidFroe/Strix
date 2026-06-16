# gsi/engine.py
from __future__ import annotations
import os, sys, json, logging, re
from typing import Any, Dict, List, Optional, Tuple

from .projects import (
    load_projects, create_project, set_url, set_desc,
    delete_project, rename_project
)
from .sheets import (
    load_credentials, build_service, extract_spreadsheet_id,
    get_sheet_id, get_sheet_meta, get_header_row, build_column_mapping,
    ensure_headers, add_sheet, summarize_tabs, get_tab_values,
)
from .normalizer import coerce_incoming_payload
from .validators import prevalidate_actions
from .actions import (
    perform_append, perform_edit_or_comment, perform_delete,
    action_create_tab, action_delete_tab, action_rename_tab, action_set_headers,
    action_find_rows, action_edit_where, action_delete_where,
    action_get_headers, action_get_tab, action_find_first_empty, action_list_tabs,
    action_swap_rows,
    ActionResult
)
from .status import build_guidance, construct_status

LOG = logging.getLogger("gpt_sheet")

def _write_json(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as out:
        json.dump(data, out, indent=2, ensure_ascii=False)

def _service_account_email() -> Optional[str]:
    try:
        creds = load_credentials()
        if creds and hasattr(creds, "service_account_email"):
            return getattr(creds, "service_account_email")
    except Exception:
        pass
    return None

def _strip_jsonc(raw: str) -> Tuple[str, Dict[str, int]]:
    # BOM
    bom = raw.startswith("\ufeff")
    if bom:
        raw = raw.lstrip("\ufeff")
    # // line comments
    before = raw
    raw = re.sub(r'(?m)^\s*//.*$', '', raw)
    line_cmts = 0 if before == raw else len(re.findall(r'(?m)^\s*//.*$', before))
    # /* ... */ block
    before = raw
    raw = re.sub(r'/\*.*?\*/', '', raw, flags=re.S)
    block_cmts = 0 if before == raw else len(re.findall(r'/\*.*?\*/', before, flags=re.S))
    # trailing commas
    before = raw
    raw = re.sub(r',\s*(\]|})', r'\1', raw)
    trailing = 0 if before == raw else 1
    return raw, {"bom": int(bom), "line_comments": line_cmts, "block_comments": block_cmts, "trailing_commas": trailing}

def _load_json_tolerant(path: str) -> Dict[str, Any]:
    LOG.debug("Using tolerant JSON loader for update.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    cleaned, stats = _strip_jsonc(raw)
    LOG.debug("update.json Pfad: %s (Bytes: %d)", path, len(raw))
    LOG.debug("JSONC-Strip: bom=%s line_comments=%d block_comments=%d trailing_commas=%d",
              bool(stats["bom"]), stats["line_comments"], stats["block_comments"], stats["trailing_commas"])
    cleaned_path = os.path.join(os.path.dirname(path), "update.cleaned.json")
    try:
        with open(cleaned_path, "w", encoding="utf-8") as out:
            out.write(cleaned)
        LOG.debug("Bereinigtes JSON nach %s geschrieben.", cleaned_path)
    except Exception:
        pass
    data = json.loads(cleaned)
    LOG.debug("update.json erfolgreich geladen (Keys: %s)", ", ".join(sorted(data.keys())))
    return data

def _decide_guidance_policy(
    results: List[Dict[str, Any]],
    diagnostics: List[Dict[str, Any]],
    request_guidance: bool,
    is_new_project: bool
) -> Dict[str, bool]:
    def has_error(diags: List[Dict[str, Any]]) -> bool:
        return any(d.get("level") == "error" for d in (diags or []))
    def is_structural(diags: List[Dict[str, Any]]) -> bool:
        structural = {
            "invalid_json", "project_missing", "project_unknown",
            "missing_worksheet", "no_headers", "invalid_sheet_url",
            "unknown_action", "missing_update_json"
        }
        return any(d.get("code") in structural for d in (diags or []))

    if is_new_project:
        return {"include_guidance": True, "guidance_available_hint": False}

    if is_structural(diagnostics):
        return {"include_guidance": True, "guidance_available_hint": False}

    failed = sum(1 for r in results if r.get("status") == "failed")
    any_error = has_error(diagnostics)

    if (not any_error) and (failed == 0):
        return {"include_guidance": bool(request_guidance), "guidance_available_hint": not request_guidance}

    return {"include_guidance": bool(request_guidance), "guidance_available_hint": not request_guidance}


# --- Quick-Start NEW/NEU helpers --------------------------------------------
def _read_raw_update(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""

def _parse_quick_new_from_raw(raw: str) -> Optional[Dict[str, str]]:
    """
    Erlaubt ein minimales update.json, das *nicht* zwingend JSON ist:
      - Erstes Wort (nach Whitespace) MUSS 'neu' (case-insensitiv) sein.
      - Irgendwo im Text MUSS eine http/https-URL stehen -> Sheet-URL
      - Zusätzlich steht ein *einzelnes* Wort als Projektname im Text.
    Beispiel:
        neu  https://docs.google.com/spreadsheets/d/......  Stellen_Kontakte
    Gibt bei Erfolg: {"project": <name>, "url": <url>} zurück, sonst None.
    """
    import re
    if not raw or not re.match(r'^\s*neu\b', raw, flags=re.IGNORECASE):
        return None

    # URL suchen (erste http/https-URL)
    m_url = re.search(r'(https?://\S+)', raw, flags=re.IGNORECASE)
    if not m_url:
        return None
    url = m_url.group(1).strip()

    # Alles außer 'neu' und URL entfernen, dann erstes "Wort" nehmen als Projektname
    text_wo_neu = re.sub(r'^\s*neu\b', '', raw, flags=re.IGNORECASE).strip()
    text_wo_url = text_wo_neu.replace(url, ' ').strip()

    # Projektname: erstes Token (ein einzelnes Wort); Umlaute/Bindestriche zulassen
    m_name = re.search(r'([A-Za-z0-9_\-ÄÖÜäöüß]+)', text_wo_url)
    project = m_name.group(1) if m_name else None
    if not project:
        project = "Neues_Projekt"

    return {"project": project, "url": url}

def _json_new_shortcut(data: dict) -> Optional[Dict[str, str]]:
    """
    Optionaler JSON-Weg: Wenn in einer *valide* geladenen JSON steht:
        { "new": true, "project": "Name", "url": "https://..." }
      oder
        { "new": true, "project": "Name", "sheet": {"url": "https://..."} }
    → Projekt sofort anlegen.
    """
    if not isinstance(data, dict) or not data.get("new"):
        return None
    project = data.get("project") or data.get("name")
    url = (data.get("sheet") or {}).get("url") or data.get("url")
    if project and url:
        return {"project": project, "url": url}
    return None





def run(args) -> None:
    update_file = os.path.join(os.getcwd(), "update.json")
    status_file = os.path.join(os.getcwd(), "status.json")
    LOG.debug("engine path: %s", os.path.abspath(__file__))
    LOG.debug("cwd: %s", os.getcwd())
    LOG.debug("argv: %s", sys.argv)
    LOG.debug("update.json: %s | status.json: %s", update_file, status_file)

    # -------- Admin-Operationen --------
    if getattr(args, "list_projects", False):
        projects = load_projects()
        print(json.dumps(projects, indent=2, ensure_ascii=False))
        _write_json(status_file, {
            "server_notes": "Projektliste ausgegeben.",
            "projects": projects,
            "version": "1.9"
        })
        return

    if getattr(args, "show", None):
        db = load_projects()
        if args.show not in db:
            print(f"Projekt nicht gefunden: {args.show}")
            _write_json(status_file, {
                "server_notes": "Projekt nicht gefunden.",
                "diagnostics": [{"level": "error", "code": "project_unknown",
                                 "message": f"Projekt '{args.show}' existiert nicht."}],
                "version": "1.9"
            })
            sys.exit(1)
        print(json.dumps({args.show: db[args.show]}, indent=2, ensure_ascii=False))
        _write_json(status_file, {
            "server_notes": "Projekt angezeigt.",
            "project": args.show,
            "project_data": db[args.show],
            "version": "1.9"
        })
        return

    if getattr(args, "set_url", None):
        if not args.url:
            print("Bitte --url angeben.")
            sys.exit(1)
        set_url(args.set_url, args.url)
        _write_json(status_file, {
            "server_notes": "Projekt-URL gesetzt.",
            "project": args.set_url,
            "sheet_url": args.url,
            "version": "1.9"
        })
        return

    if getattr(args, "set_desc", None):
        if args.desc is None:
            print("Bitte --desc angeben.")
            sys.exit(1)
        set_desc(args.set_desc, args.desc)
        _write_json(status_file, {
            "server_notes": "Projektbeschreibung gesetzt.",
            "project": args.set_desc,
            "description": args.desc,
            "version": "1.9"
        })
        return

    if getattr(args, "delete", None):
        delete_project(args.delete)
        _write_json(status_file, {
            "server_notes": "Projekt gelöscht.",
            "project": args.delete,
            "version": "1.9"
        })
        return

    if getattr(args, "rename", None):
        if not args.to:
            print("Bitte --to <NeuerName> angeben.")
            sys.exit(1)
        rename_project(args.rename, args.to)
        _write_json(status_file, {
            "server_notes": "Projekt umbenannt.",
            "from": args.rename, "to": args.to,
            "version": "1.9"
        })
        return

    if getattr(args, "new", None):
        try:
            create_project(args.new, url=args.url or "", desc=args.desc or "")
            new_created = True
        except ValueError:
            print(f"Projekt '{args.new}' existiert bereits – benutze bestehendes Projekt.")
            new_created = False

        sa_mail = _service_account_email()
        sheet_url = args.url or ""
        tabs = []
        title = None
        try:
            if sheet_url:
                creds = load_credentials()
                service = build_service(creds) if creds else None
                if service:
                    sid = extract_spreadsheet_id(sheet_url)
                    if sid:
                        meta = get_sheet_meta(service, sid)
                        title = (meta.get("properties") or {}).get("title")
                        tabs = summarize_tabs(service, sid)
        except Exception:
            pass

        flags_default = {
            "allow_auto_tab_create": False,
            "allow_auto_headers": True,
            "include_snapshots": True,
            "autocreate_project": False
        }
        guidance = build_guidance(args.new, sheet_url, None, None, [], [], tabs, flags_default, sa_mail)
        status = construct_status(
            project=args.new,
            update_id=None,
            sheet_url=sheet_url,
            results=[], diagnostics=[],
            guidance=guidance,
            tabs=tabs, sheet_events=[], spreadsheet_title=title,
            context={"project_cli": args.new, "project_update": None, "project_effective": args.new, "project_source": "cli"},
            include_guidance=True
        )
        _write_json(status_file, status)
        if new_created:
            print(f"Projekt angelegt: {args.new}")
        return

    # -------- Normaler Run --------
    db = load_projects()
    LOG.debug("Projekte geladen: %d (%s)", len(db), ", ".join(db.keys()))
    diagnostics: List[Dict[str, Any]] = []
    sheet_events: List[Dict[str, Any]] = []
    update_data: Optional[Dict[str, Any]] = None

    # --- Quick-Start: "neu" am Dateibeginn (nicht zwingend JSON) -------------
    raw_probe = _read_raw_update(update_file)
    quick_new = _parse_quick_new_from_raw(raw_probe) if raw_probe else None
    if quick_new:
        proj = quick_new["project"]
        url = quick_new["url"]

        # Projekt anlegen (falls existent: überschreiben wir es *nicht*)
        if proj not in db:
            create_project(proj, url=url, desc="Keine Angaben")
            db = load_projects()
            diagnostics.append({"level": "info", "code": "project_created_quick", "message": f"Projekt '{proj}' via Quick-Start angelegt."})
        else:
            # Nur URL setzen, wenn leer
            if not db[proj].get("url"):
                set_url(proj, url)
                db = load_projects()
                diagnostics.append({"level": "info", "code": "project_url_set", "message": f"URL für bestehendes Projekt '{proj}' gesetzt."})

        # Guidance/Status sofort zurückgeben
        selected_project = proj
        sheet_url_final = url
        worksheet_name = None

        sa_mail = _service_account_email()
        try:
            creds = load_credentials()
            service = build_service(creds) if creds else None
            sid = extract_spreadsheet_id(sheet_url_final) if (service and sheet_url_final) else None
            tabs = summarize_tabs(service, sid) if sid else []
            meta = get_sheet_meta(service, sid) if sid else {}
            spreadsheet_title = (meta.get("properties") or {}).get("title")
        except Exception:
            tabs, spreadsheet_title = [], None

        flags = {
            "allow_auto_tab_create": False,
            "allow_auto_headers": True,
            "include_snapshots": True,
            "autocreate_project": False
        }
        guidance = build_guidance(selected_project, sheet_url_final, worksheet_name, None, diagnostics, sheet_events, tabs, flags, sa_mail)
        status = construct_status(
            project=selected_project,
            update_id=None,
            sheet_url=sheet_url_final,
            results=[], diagnostics=diagnostics, guidance=guidance,
            tabs=tabs, sheet_events=sheet_events, spreadsheet_title=spreadsheet_title,
            context={"project_cli": None, "project_update": None, "project_effective": selected_project, "project_source": "quick_new"},
            include_guidance=True
        )
        _write_json(status_file, status)
        LOG.debug("status.json geschrieben → %s", status_file)
        return

    try:
        update_data = _load_json_tolerant(update_file)
    except FileNotFoundError:
        diagnostics.append({"level": "error", "code": "missing_update_json", "message": "update.json nicht gefunden."})
    except json.JSONDecodeError as exc:
        diagnostics.append({"level": "error", "code": "invalid_json", "message": f"update.json ungültig: {exc}"})


  # JSON-Shortcut: { "new": true, "project": "Name", "url": "..." }
    json_new = _json_new_shortcut(update_data) if isinstance(update_data, dict) else None
    if json_new:
        proj = json_new["project"]
        url = json_new["url"]

        if proj not in db:
            create_project(proj, url=url, desc="Keine Angaben")
            db = load_projects()
            diagnostics.append({"level": "info", "code": "project_created_json", "message": f"Projekt '{proj}' via JSON-Shortcut angelegt."})
        else:
            if not db[proj].get("url"):
                set_url(proj, url)
                db = load_projects()
                diagnostics.append({"level": "info", "code": "project_url_set", "message": f"URL für bestehendes Projekt '{proj}' gesetzt."})

        # Sofort Guidance zurückliefern
        selected_project = proj
        sheet_url_final = url
        worksheet_name = None

        sa_mail = _service_account_email()
        try:
            creds = load_credentials()
            service = build_service(creds) if creds else None
            sid = extract_spreadsheet_id(sheet_url_final) if (service and sheet_url_final) else None
            tabs = summarize_tabs(service, sid) if sid else []
            meta = get_sheet_meta(service, sid) if sid else {}
            spreadsheet_title = (meta.get("properties") or {}).get("title")
        except Exception:
            tabs, spreadsheet_title = [], None

        flags = {
            "allow_auto_tab_create": False,
            "allow_auto_headers": True,
            "include_snapshots": True,
            "autocreate_project": False
        }
        guidance = build_guidance(selected_project, sheet_url_final, worksheet_name, None, diagnostics, sheet_events, tabs, flags, sa_mail)
        status = construct_status(
            project=selected_project,
            update_id=None,
            sheet_url=sheet_url_final,
            results=[], diagnostics=diagnostics, guidance=guidance,
            tabs=tabs, sheet_events=sheet_events, spreadsheet_title=spreadsheet_title,
            context={"project_cli": None, "project_update": None, "project_effective": selected_project, "project_source": "json_new"},
            include_guidance=True
        )
        _write_json(status_file, status)
        LOG.debug("status.json geschrieben → %s", status_file)
        return

    # Convenience-Defaults
    if isinstance(update_data, dict):
        if "help" not in update_data:
            diagnostics.append({"level": "info", "code": "help_default_false", "message": "Feld 'help' fehlte – auf false gesetzt."})
            update_data["help"] = False

    request_guidance = bool((update_data or {}).get("request_guidance", False))
    LOG.debug("request_guidance=%s", request_guidance)

    # Projekt ermitteln
    selected_project: Optional[str] = None
    source = None
    if getattr(args, "project", None):
        selected_project = args.project; source = "cli"
    elif update_data and isinstance(update_data.get("project"), str):
        selected_project = update_data.get("project"); source = "update_json"
        diagnostics.append({"level": "info", "code": "project_from_update",
                            "message": f"Projekt aus update.json: '{selected_project}'."})
    else:
        diagnostics.append({"level": "error", "code": "project_missing",
                            "message": "Kein Projekt angegeben (weder CLI --project noch update.json.project)."})

    # autocreate_project (Top-Level ODER flags{})
    flags_top = (update_data or {})
    flags_obj = (update_data.get("flags") or {}) if isinstance(update_data, dict) else {}
    autocreate_project = bool(flags_top.get("autocreate_project", flags_obj.get("autocreate_project", False)))
    LOG.debug("selected_project=%s (source=%s)", selected_project, source)
    LOG.debug("autocreate_project=%s", autocreate_project)

    if selected_project and selected_project not in db:
        if autocreate_project and update_data:
            url_from_update = (update_data.get("sheet") or {}).get("url") or ""
            desc_from_update = update_data.get("notes") or ""
            create_project(selected_project, url=url_from_update, desc=desc_from_update)
            db = load_projects()
            diagnostics.append({"level": "warning", "code": "project_autocreated",
                                "message": f"Projekt '{selected_project}' aus update.json angelegt."})
        else:
            diagnostics.append({"level": "error", "code": "project_unknown",
                                "message": f"Projekt '{selected_project}' existiert nicht. "
                                           f"Nutze --new oder setze 'autocreate_project': true in update.json."})

    context: Dict[str, Any] = {
        "project_cli": args.project if getattr(args, "project", None) else None,
        "project_update": (update_data or {}).get("project") if update_data else None,
        "project_effective": selected_project,
        "project_source": source,
        "flags": {"autocreate_project": autocreate_project}
    }

    # Harte Frühfehler
    if any(d["level"] == "error" and d["code"] in {
        "project_missing", "project_unknown", "missing_update_json", "invalid_json"
    } for d in diagnostics):
        sa_mail = _service_account_email()
        guidance = build_guidance(selected_project, None, None, None, diagnostics, sheet_events, [], {}, sa_mail)
        policy = _decide_guidance_policy([], diagnostics, request_guidance, is_new_project=False)
        status = construct_status(
            selected_project,
            (update_data or {}).get("update_id") if isinstance(update_data, dict) else None,
            None, [], diagnostics, guidance, [], sheet_events, None, context,
            include_guidance=policy["include_guidance"],
            guidance_available_hint=policy["guidance_available_hint"]
        )
        _write_json(status_file, status)
        if any(d["code"] == "project_missing" for d in diagnostics):
            print("Bitte Projekt angeben: --project <Name> ODER 'project' in update.json setzen.")
        return

    # URL priorisieren
    project_url = db[selected_project].get("url") if selected_project else ""
    req_url = ((update_data or {}).get("sheet") or {}).get("url") if update_data else ""
    if getattr(args, "force_project", False):
        sheet_url_final = project_url
        if req_url and req_url != project_url:
            diagnostics.append({"level": "warning", "code": "force_project_url",
                                "message": "update.json URL ignoriert; Projekt-URL erzwungen."})
    else:
        sheet_url_final = req_url or project_url
        if req_url and project_url and req_url != project_url:
            diagnostics.append({"level": "warning", "code": "url_conflict",
                                "message": "update.json URL ≠ Projekt-URL; benutze update.json URL."})
    LOG.debug("sheet_url_final=%s (req_url=%s, project_url=%s)", sheet_url_final, req_url, project_url)

    # Worksheet bestimmen
    worksheet_name = ((update_data or {}).get("sheet") or {}).get("worksheet") if update_data else None
    worksheet_name = (update_data or {}).get("worksheet", worksheet_name)
    LOG.debug("worksheet_name=%s", worksheet_name)

    # Help-Modus → nur Guidance
    if update_data and update_data.get("help", False) is True:
        sa_mail = _service_account_email()
        tabs = []
        spreadsheet_title = None
        if sheet_url_final:
            creds = load_credentials()
            try:
                service = build_service(creds) if creds else None
                if service:
                    sid = extract_spreadsheet_id(sheet_url_final)
                    if sid:
                        meta = get_sheet_meta(service, sid)
                        spreadsheet_title = (meta.get("properties") or {}).get("title")
                        tabs = summarize_tabs(service, sid)
            except Exception:
                pass
        # Flags aus Top-Level ODER flags{}
        allow_auto_tab_create = bool(flags_top.get("allow_auto_tab_create", flags_obj.get("allow_auto_tab_create", False)))
        allow_auto_headers = bool(flags_top.get("allow_auto_headers", flags_obj.get("allow_auto_headers", True)))
        include_snapshots = bool(flags_top.get("include_snapshots", flags_obj.get("include_snapshots", True)))
        flags_dump = {
            "allow_auto_tab_create": allow_auto_tab_create,
            "allow_auto_headers": allow_auto_headers,
            "include_snapshots": include_snapshots,
            "autocreate_project": autocreate_project,
        }
        guidance = build_guidance(selected_project, sheet_url_final, worksheet_name, None, diagnostics, sheet_events, tabs, flags_dump, sa_mail)
        status = construct_status(selected_project, update_data.get("update_id"), sheet_url_final, [],
                                  diagnostics, guidance, tabs, sheet_events, spreadsheet_title, context,
                                  include_guidance=True)
        _write_json(status_file, status)
        print("Help-Mode: Guidance in status.json geschrieben.")
        return

    if not sheet_url_final:
        diagnostics.append({"level": "error", "code": "missing_url",
                            "message": "`sheet.url` fehlt (oder Projekt-URL leer)."})
        sa_mail = _service_account_email()
        guidance = build_guidance(selected_project, sheet_url_final, worksheet_name, None, diagnostics, sheet_events)
        policy = _decide_guidance_policy([], diagnostics, request_guidance, is_new_project=False)
        status = construct_status(selected_project, (update_data or {}).get("update_id"), sheet_url_final,
                                  [], diagnostics, guidance, [], sheet_events, None, context,
                                  include_guidance=policy["include_guidance"],
                                  guidance_available_hint=policy["guidance_available_hint"])
        _write_json(status_file, status)
        return

    # Flags (Top-Level ODER flags{})
    allow_auto_tab_create = bool(flags_top.get("allow_auto_tab_create", flags_obj.get("allow_auto_tab_create", False)))
    allow_auto_headers    = bool(flags_top.get("allow_auto_headers",    flags_obj.get("allow_auto_headers",    True)))
    include_snapshots     = bool(flags_top.get("include_snapshots",     flags_obj.get("include_snapshots",     True)))
    context["flags"].update({
        "allow_auto_tab_create": allow_auto_tab_create,
        "allow_auto_headers": allow_auto_headers,
        "include_snapshots": include_snapshots,
    })
    LOG.debug("flags: %s", context["flags"])

    # Auth + Service
    creds = load_credentials()
    if creds is None or build_service is None:
        diagnostics.append({"level": "error", "code": "no_credentials",
                            "message": "Keine Google-Credentials oder Libraries vorhanden."})
        sa_mail = _service_account_email()
        guidance = build_guidance(selected_project, sheet_url_final, worksheet_name, None, diagnostics, sheet_events)
        policy = _decide_guidance_policy([], diagnostics, request_guidance, is_new_project=False)
        status = construct_status(selected_project, (update_data or {}).get("update_id"), sheet_url_final,
                                  [], diagnostics, guidance, [], sheet_events, None, context,
                                  include_guidance=policy["include_guidance"],
                                  guidance_available_hint=policy["guidance_available_hint"])
        _write_json(status_file, status)
        return
    try:
        service = build_service(creds)
        LOG.debug("Sheets-Service initialisiert.")
    except Exception as exc:
        diagnostics.append({"level": "error", "code": "service_init_failed",
                            "message": f"Fehler beim Initialisieren der Sheets API: {exc}"})
        sa_mail = _service_account_email()
        guidance = build_guidance(selected_project, sheet_url_final, worksheet_name, None, diagnostics, sheet_events)
        policy = _decide_guidance_policy([], diagnostics, request_guidance, is_new_project=False)
        status = construct_status(selected_project, (update_data or {}).get("update_id"), sheet_url_final,
                                  [], diagnostics, guidance, [], sheet_events, None, context,
                                  include_guidance=policy["include_guidance"],
                                  guidance_available_hint=policy["guidance_available_hint"])
        _write_json(status_file, status)
        return

    spreadsheet_id = extract_spreadsheet_id(sheet_url_final)
    if not spreadsheet_id:
        diagnostics.append({"level": "error", "code": "invalid_sheet_url",
                            "message": "Konnte Spreadsheet ID nicht aus der URL extrahieren."})
        sa_mail = _service_account_email()
        guidance = build_guidance(selected_project, sheet_url_final, worksheet_name, None, diagnostics, sheet_events)
        policy = _decide_guidance_policy([], diagnostics, request_guidance, is_new_project=False)
        status = construct_status(selected_project, (update_data or {}).get("update_id"), sheet_url_final,
                                  [], diagnostics, guidance, [], sheet_events, None, context,
                                  include_guidance=policy["include_guidance"],
                                  guidance_available_hint=policy["guidance_available_hint"])
        _write_json(status_file, status)
        return

    # Spreadsheet-Meta + Tabs
    meta = get_sheet_meta(service, spreadsheet_id)
    spreadsheet_title = (meta.get("properties") or {}).get("title")
    tabs_now = summarize_tabs(service, spreadsheet_id)
    LOG.debug("Tabs jetzt: %s", ", ".join(t.get("title", "") for t in (tabs_now or [])) or "(keine)")

    # Worksheet defaulten, falls nur 1 Tab existiert
    if not worksheet_name and isinstance(tabs_now, list) and len(tabs_now) == 1:
        worksheet_name = tabs_now[0].get("title")
        diagnostics.append({"level": "info", "code": "worksheet_autopicked",
                            "message": f"Worksheet automatisch gewählt: '{worksheet_name}'."})

    # --- Aktionen früh normalisieren (ops → actions), damit needs_ws_context korrekt ist ---
    raw_update = update_data or {}
    update_data = coerce_incoming_payload(raw_update, None, diagnostics)  # headers noch None
    actions = update_data.get("actions", [])
    LOG.debug("actions: %d → Typen=%s", len(actions),
              ", ".join(sorted({a.get("type") for a in actions})) if actions else "(keine)")


    needs_ws_context = any(a.get("type") in {
        "append","append_rows","edit","delete","comment",
        "edit_where","delete_where","find_first_empty","get_tab","get_headers",
        "swap_rows",
    } for a in actions)

    headers: List[str] | None = None
    col_map: Dict[str, int] = {}
    header_count = 0

    if needs_ws_context:
        if not worksheet_name:
            diagnostics.append({"level": "error", "code": "missing_worksheet",
                                "message": "`sheet.worksheet` fehlt."})
            sa_mail = _service_account_email()
            guidance = build_guidance(selected_project, sheet_url_final, worksheet_name, None, diagnostics, sheet_events)
            policy = _decide_guidance_policy([], diagnostics, request_guidance, is_new_project=False)
            status = construct_status(selected_project, (update_data or {}).get("update_id"), sheet_url_final,
                                      [], diagnostics, guidance, tabs_now, sheet_events, spreadsheet_title, context,
                                      include_guidance=policy["include_guidance"],
                                      guidance_available_hint=policy["guidance_available_hint"])
            _write_json(status_file, status)
            return

        sid = get_sheet_id(service, spreadsheet_id, worksheet_name)
        if sid is None:
            if allow_auto_tab_create:
                add_sheet(service, spreadsheet_id, worksheet_name)
                sheet_events.append({"event": "worksheet_created_auto", "worksheet": worksheet_name})
            else:
                diagnostics.append({"level": "error", "code": "worksheet_missing",
                                    "message": f"Worksheet '{worksheet_name}' fehlt. "
                                               f"Verwende 'create_tab' oder setze allow_auto_tab_create=true."})
                sa_mail = _service_account_email()
                guidance = build_guidance(selected_project, sheet_url_final, worksheet_name, None, diagnostics, sheet_events)
                policy = _decide_guidance_policy([], diagnostics, request_guidance, is_new_project=False)
                status = construct_status(selected_project, (update_data or {}).get("update_id"), sheet_url_final,
                                          [], diagnostics, guidance, tabs_now, sheet_events, spreadsheet_title, context,
                                          include_guidance=policy["include_guidance"],
                                          guidance_available_hint=policy["guidance_available_hint"])
                _write_json(status_file, status)
                return

        LOG.debug("Reading header row from %s!1:1 …", worksheet_name)
        headers = get_header_row(service, spreadsheet_id, worksheet_name)

        # Header ggf. initialisieren
        if not headers:
            if allow_auto_headers:
                schema = (update_data or {}).get("schema")
                if not isinstance(schema, list) or not schema:
                    first_append = next((a for a in actions if a.get("type") in {"append", "append_rows"}), None)
                    if first_append:
                        if first_append.get("type") == "append" and isinstance(first_append.get("values"), dict):
                            schema = list(first_append["values"].keys())
                        elif (first_append.get("type") == "append_rows"
                              and isinstance(first_append.get("rows"), list) and first_append["rows"]):
                            schema = list(first_append["rows"][0].keys())
                if not schema:
                    diagnostics.append({"level": "error", "code": "no_headers",
                                        "message": "Keine Header vorhanden und kein Schema ableitbar. "
                                                   "Gib 'schema' an oder liefere 'append/append_rows' mit Daten."})
                    sa_mail = _service_account_email()
                    guidance = build_guidance(selected_project, sheet_url_final, worksheet_name, None, diagnostics, sheet_events)
                    policy = _decide_guidance_policy([], diagnostics, request_guidance, is_new_project=False)
                    status = construct_status(selected_project, (update_data or {}).get("update_id"), sheet_url_final,
                                              [], diagnostics, guidance, tabs_now, sheet_events, spreadsheet_title, context,
                                              include_guidance=policy["include_guidance"],
                                              guidance_available_hint=policy["guidance_available_hint"])
                    _write_json(status_file, status)
                    return

                ensure_headers(service, spreadsheet_id, worksheet_name, schema)
                headers = schema
                sheet_events.append({"event": "headers_initialized_auto", "worksheet": worksheet_name, "schema": schema})
            else:
                diagnostics.append({"level": "error", "code": "headers_missing_auto_off",
                                    "message": "Header fehlen, 'allow_auto_headers' ist jedoch False."})
                sa_mail = _service_account_email()
                guidance = build_guidance(selected_project, sheet_url_final, worksheet_name, None, diagnostics, sheet_events)
                policy = _decide_guidance_policy([], diagnostics, request_guidance, is_new_project=False)
                status = construct_status(selected_project, (update_data or {}).get("update_id"), sheet_url_final,
                                          [], diagnostics, guidance, tabs_now, sheet_events, spreadsheet_title, context,
                                          include_guidance=policy["include_guidance"],
                                          guidance_available_hint=policy["guidance_available_hint"])
                _write_json(status_file, status)
                return

        # Spaltenmapping & Headeranzahl EINMALIG setzen
        col_map = build_column_mapping(headers) if headers else {}
        header_count = len(headers or [])
        LOG.debug("col_map=%s | header_count=%d", col_map, header_count)

    # Vorvalidieren (ohne erneutes coerce!)
    pre_results, remaining = prevalidate_actions(actions, headers, worksheet_name, diagnostics)
    LOG.debug("prevalidate: pre=%d remaining=%d", len(pre_results), len(remaining))
    results: List[Dict[str, Any]] = [r.to_dict() for r in pre_results]

    # Ausführen
    for idx, act in enumerate(remaining, start=1):
        t = act["type"]
        ws = act.get("worksheet", worksheet_name)

        if t == "append":
            res = perform_append(
                service, spreadsheet_id, ws, col_map, header_count,
                act.get("values", {}), bool(update_data.get("include_snapshots", True))
            )

        elif t == "append_rows":
            row_dicts = act.get("rows", [])
            if not isinstance(row_dicts, list) or not row_dicts:
                res = ActionResult("append_rows", None, "failed", "rows[] fehlt oder ist leer")
            else:
                ok = 0
                for rvals in row_dicts:
                    res_i = perform_append(
                        service, spreadsheet_id, ws, col_map, header_count,
                        rvals, bool(update_data.get("include_snapshots", True))
                    )
                    results.append(res_i.to_dict())
                    if res_i.status == "success":
                        ok += 1
                failed = len(row_dicts) - ok
                res = ActionResult(
                    "append_rows", None,
                    "success" if ok > 0 else "failed",
                    None if ok > 0 else "Alle Einträge fehlgeschlagen",
                    {"count": len(row_dicts), "ok": ok, "failed": failed, "worksheet": ws}
                )

        elif t in ("edit", "comment"):
            res = perform_edit_or_comment(
                service, spreadsheet_id, ws, col_map, t,
                act.get("row"), act.get("values", {}),
                bool(update_data.get("include_snapshots", True))
            )

        elif t == "delete":
            sid = get_sheet_id(service, spreadsheet_id, ws)
            res = perform_delete(
                service, spreadsheet_id, sid, ws, act.get("row"),
                header_count, bool(update_data.get("include_snapshots", True))
            )

        elif t == "create_tab":
            res = action_create_tab(service, spreadsheet_id, act["worksheet"], act.get("schema"), act.get("index"))
            sheet_events.append({"event": "worksheet_created", "worksheet": act["worksheet"],
                                 "schema": act.get("schema"), "index": act.get("index")})

        elif t == "delete_tab":
            res = action_delete_tab(service, spreadsheet_id, act["worksheet"])
            sheet_events.append({"event": "worksheet_deleted", "worksheet": act["worksheet"]})

        elif t == "rename_tab":
            res = action_rename_tab(service, spreadsheet_id, act["worksheet"], act["new_name"])
            sheet_events.append({"event": "worksheet_renamed", "from": act["worksheet"], "to": act["new_name"]})

        elif t == "set_headers":
            res = action_set_headers(service, spreadsheet_id, act["worksheet"], act["schema"])
            sheet_events.append({"event": "headers_set", "worksheet": act["worksheet"], "schema": act["schema"]})

        elif t == "list_tabs":
            res = action_list_tabs(service, spreadsheet_id)

        elif t == "find_rows":
            res = action_find_rows(
                service, spreadsheet_id, ws,
                where=act.get("where", {}),
                limit=act.get("limit"),
                offset=int(act.get("offset", 0)),
            )

        elif t == "edit_where":
            res = action_edit_where(
                service, spreadsheet_id, ws, col_map,
                values=act.get("values", {}),
                where=act.get("where", {}),
                header_count=header_count,
                limit=act.get("limit"),
                offset=int(act.get("offset", 0)),
                include_snapshots=bool(update_data.get("include_snapshots", True)),
            )

        elif t == "get_headers":
            res = action_get_headers(service, spreadsheet_id, ws)

        elif t == "get_tab":
            res = action_get_tab(
                service, spreadsheet_id, ws,
                limit=act.get("limit"), offset=int(act.get("offset", 0)),
                as_objects=bool(act.get("as_objects", False)),
            )

        elif t == "find_first_empty":
            res = action_find_first_empty(
                service, spreadsheet_id, ws,
                header=act.get("header"),
                start_row=int(act.get("start_row", 2)),
            )

        elif t == "swap_rows":
            res = action_swap_rows(
                service, spreadsheet_id, ws,
                row_a=int(act.get("row_a", 0)),
                row_b=int(act.get("row_b", 0)),
            )

        elif t == "delete_where":
            res = action_delete_where(
                service, spreadsheet_id, ws,
                where=act.get("where", {}),
                header_count=header_count,
                limit=act.get("limit"),
                offset=int(act.get("offset", 0)),
                include_snapshots=bool(update_data.get("include_snapshots", True)),
            )
        else:
            res = ActionResult(t, None, "failed", f"Unbekannter Aktionstyp '{t}'")

        if res is not None:
            results.append(res.to_dict())

    # Status zusammenbauen
    sa_mail = _service_account_email()
    policy = _decide_guidance_policy(results, diagnostics, request_guidance, is_new_project=False)
    # summarize_tabs nur nochmal wenn Guidance gewünscht – sonst tabs_now wiederverwenden
    tabs_final = summarize_tabs(service, spreadsheet_id) if (request_guidance or policy["include_guidance"]) else tabs_now
    guidance = build_guidance(
        selected_project, sheet_url_final, worksheet_name,
        headers if headers else None,
        diagnostics, sheet_events,
        tabs=tabs_final,
        flags=context.get("flags", {}), service_account_email=sa_mail
    )
    status = construct_status(
        project=selected_project,
        update_id=update_data.get("update_id") if isinstance(update_data, dict) else None,
        sheet_url=sheet_url_final,
        results=results, diagnostics=diagnostics, guidance=guidance,
        tabs=tabs_final,
        sheet_events=sheet_events,
        spreadsheet_title=spreadsheet_title,
        context=context,
        include_guidance=policy["include_guidance"],
        guidance_available_hint=policy["guidance_available_hint"]
    )
    _write_json(status_file, status)
    LOG.debug("status.json geschrieben → %s", status_file)


# ─────────────────────────── In-Process API (Prio 1) ──────────────────────────

def run_payload(payload: dict) -> dict:
    """
    Direkte In-Process-Ausführung ohne subprocess und Datei-I/O.
    Nimmt Payload-Dict, gibt Status-Dict zurück.
    Nutzt gecachten Google Service aus sheets.get_cached_service().
    """
    from .sheets import (
        get_cached_service, extract_spreadsheet_id, get_sheet_meta, get_sheet_id,
        get_header_row, build_column_mapping, ensure_headers, add_sheet, summarize_tabs,
    )

    diagnostics: List[Dict[str, Any]] = []
    sheet_events: List[Dict[str, Any]] = []

    payload = coerce_incoming_payload(dict(payload), None, diagnostics)
    if "help" not in payload:
        diagnostics.append({"level": "info", "code": "help_default_false",
                            "message": "Feld 'help' fehlte – auf false gesetzt."})
        payload["help"] = False

    request_guidance  = bool(payload.get("request_guidance", False))
    include_snapshots = bool(payload.get("include_snapshots", False))
    allow_auto_headers    = bool(payload.get("allow_auto_headers", True))
    allow_auto_tab_create = bool(payload.get("allow_auto_tab_create", False))

    def _fail_early(diags, project=None, url=None, ws=None, tabs=None):
        g = build_guidance(project, url, ws, None, diags, [], tabs or [], {}, _service_account_email())
        pol = _decide_guidance_policy([], diags, request_guidance, False)
        return construct_status(project, payload.get("update_id"), url, [], diags, g,
                               tabs or [], [], None, {}, include_guidance=pol["include_guidance"])

    # ── Projekt ────────────────────────────────────────────────────────────────
    selected_project = payload.get("project")
    if not selected_project:
        diagnostics.append({"level": "error", "code": "project_missing",
                            "message": "Kein Projekt angegeben (update.json.project fehlt)."})
        return _fail_early(diagnostics)

    db = load_projects()
    if selected_project not in db:
        diagnostics.append({"level": "error", "code": "project_unknown",
                            "message": f"Projekt '{selected_project}' unbekannt."})
        return _fail_early(diagnostics, project=selected_project)

    # ── Sheet-URL + Worksheet ──────────────────────────────────────────────────
    req_url      = (payload.get("sheet") or {}).get("url", "")
    sheet_url    = req_url or db[selected_project].get("url", "")
    worksheet_name = (payload.get("sheet") or {}).get("worksheet") or payload.get("worksheet")

    context: Dict[str, Any] = {
        "project_cli": None, "project_update": selected_project,
        "project_effective": selected_project, "project_source": "update_json",
        "flags": {
            "include_snapshots": include_snapshots,
            "allow_auto_headers": allow_auto_headers,
            "allow_auto_tab_create": allow_auto_tab_create,
            "autocreate_project": False,
        },
    }

    if not sheet_url:
        diagnostics.append({"level": "error", "code": "missing_url",
                            "message": "`sheet.url` fehlt (und Projekt-URL leer)."})
        return _fail_early(diagnostics, project=selected_project)

    # ── Service (gecacht) ──────────────────────────────────────────────────────
    service = get_cached_service()
    if service is None:
        diagnostics.append({"level": "error", "code": "no_credentials",
                            "message": "Google-Credentials nicht verfügbar."})
        return _fail_early(diagnostics, project=selected_project, url=sheet_url)

    spreadsheet_id = extract_spreadsheet_id(sheet_url)
    if not spreadsheet_id:
        diagnostics.append({"level": "error", "code": "invalid_sheet_url",
                            "message": "Spreadsheet-ID aus URL nicht lesbar."})
        return _fail_early(diagnostics, project=selected_project, url=sheet_url)

    # ── Metadaten einmalig laden ───────────────────────────────────────────────
    try:
        meta            = get_sheet_meta(service, spreadsheet_id)
        spreadsheet_title = (meta.get("properties") or {}).get("title")
        tabs_now        = summarize_tabs(service, spreadsheet_id)
    except Exception as exc:
        diagnostics.append({"level": "error", "code": "sheet_meta_failed",
                            "message": f"Spreadsheet nicht lesbar: {exc}"})
        return _fail_early(diagnostics, project=selected_project, url=sheet_url)

    # Auto-pick Worksheet falls nur 1 Tab
    if not worksheet_name and len(tabs_now) == 1:
        worksheet_name = tabs_now[0].get("title")
        diagnostics.append({"level": "info", "code": "worksheet_autopicked",
                            "message": f"Worksheet automatisch gewählt: '{worksheet_name}'."})

    # ── Help-Modus ─────────────────────────────────────────────────────────────
    if payload.get("help") is True:
        g = build_guidance(selected_project, sheet_url, worksheet_name, None,
                          diagnostics, sheet_events, tabs_now, context["flags"], _service_account_email())
        return construct_status(selected_project, payload.get("update_id"), sheet_url,
                               [], diagnostics, g, tabs_now, [], spreadsheet_title, context, include_guidance=True)

    # ── Actions ────────────────────────────────────────────────────────────────
    actions = payload.get("actions", [])
    needs_ws = any(a.get("type") in {
        "append", "append_rows", "edit", "delete", "comment",
        "edit_where", "delete_where", "find_first_empty",
        "get_tab", "get_headers", "find_rows", "swap_rows",
    } for a in actions)

    headers: Optional[List[str]] = None
    col_map: Dict[str, str]      = {}
    header_count                 = 0
    cached_vals: Optional[List[List[str]]] = None

    if needs_ws:
        if not worksheet_name:
            diagnostics.append({"level": "error", "code": "missing_worksheet",
                                "message": "`sheet.worksheet` fehlt."})
            return _fail_early(diagnostics, project=selected_project, url=sheet_url,
                               ws=worksheet_name, tabs=tabs_now)

        ws_id = get_sheet_id(service, spreadsheet_id, worksheet_name)
        if ws_id is None:
            if allow_auto_tab_create:
                add_sheet(service, spreadsheet_id, worksheet_name)
                sheet_events.append({"event": "worksheet_created_auto", "worksheet": worksheet_name})
            else:
                diagnostics.append({"level": "error", "code": "worksheet_missing",
                                    "message": f"Worksheet '{worksheet_name}' nicht gefunden."})
                return _fail_early(diagnostics, project=selected_project, url=sheet_url,
                                   ws=worksheet_name, tabs=tabs_now)

        # Tab einmal vorab laden – spart N API-Calls für N nachfolgende Actions
        cached_vals: Optional[List[List[str]]] = get_tab_values(service, spreadsheet_id, worksheet_name)
        headers = cached_vals[0] if cached_vals else []
        if not headers:
            if allow_auto_headers:
                first_ap = next((a for a in actions if a.get("type") in {"append", "append_rows"}), None)
                schema: Optional[List[str]] = None
                if first_ap:
                    if first_ap.get("type") == "append" and isinstance(first_ap.get("values"), dict):
                        schema = list(first_ap["values"].keys())
                    elif first_ap.get("type") == "append_rows" and isinstance(first_ap.get("rows"), list) and first_ap["rows"]:
                        schema = list(first_ap["rows"][0].keys())
                if not schema:
                    diagnostics.append({"level": "error", "code": "no_headers",
                                        "message": "Keine Header und kein Schema ableitbar."})
                    return _fail_early(diagnostics, project=selected_project, url=sheet_url,
                                       ws=worksheet_name, tabs=tabs_now)
                ensure_headers(service, spreadsheet_id, worksheet_name, schema)
                headers = schema
                cached_vals = [schema]
                sheet_events.append({"event": "headers_initialized_auto", "worksheet": worksheet_name, "schema": schema})
            else:
                diagnostics.append({"level": "error", "code": "headers_missing_auto_off",
                                    "message": "Header fehlen und allow_auto_headers=False."})
                return _fail_early(diagnostics, project=selected_project, url=sheet_url,
                                   ws=worksheet_name, tabs=tabs_now)

        col_map      = build_column_mapping(headers) if headers else {}
        header_count = len(headers)

    pre_results, remaining = prevalidate_actions(actions, headers, worksheet_name, diagnostics)
    results: List[Dict[str, Any]] = [r.to_dict() for r in pre_results]

    # Actions sequenziell ausführen; cached_vals nach Schreiboperationen invalidieren
    for act in remaining:
        t   = act["type"]
        ws  = act.get("worksheet", worksheet_name)
        res = None
        # Cache nur für den primären Worksheet verwenden
        tab_cache = cached_vals if (ws == worksheet_name) else None

        if t == "append":
            res = perform_append(service, spreadsheet_id, ws, col_map, header_count,
                                act.get("values", {}), include_snapshots)
            cached_vals = None
        elif t == "append_rows":
            row_dicts = act.get("rows", [])
            ok_n = 0
            for rv in row_dicts:
                ri = perform_append(service, spreadsheet_id, ws, col_map, header_count,
                                   rv, include_snapshots)
                results.append(ri.to_dict())
                if ri.status == "success":
                    ok_n += 1
            res = ActionResult("append_rows", None,
                              "success" if ok_n > 0 else "failed",
                              None if ok_n > 0 else "Alle fehlgeschlagen",
                              {"count": len(row_dicts), "ok": ok_n, "failed": len(row_dicts) - ok_n})
            cached_vals = None
        elif t in ("edit", "comment"):
            res = perform_edit_or_comment(service, spreadsheet_id, ws, col_map, t,
                                         act.get("row"), act.get("values", {}), include_snapshots)
            cached_vals = None
        elif t == "delete":
            s_id = get_sheet_id(service, spreadsheet_id, ws)
            res = perform_delete(service, spreadsheet_id, s_id, ws,
                                act.get("row"), header_count, include_snapshots)
            cached_vals = None
        elif t == "create_tab":
            res = action_create_tab(service, spreadsheet_id, act["worksheet"],
                                   act.get("schema"), act.get("index"))
            sheet_events.append({"event": "worksheet_created", "worksheet": act["worksheet"]})
            cached_vals = None
        elif t == "delete_tab":
            res = action_delete_tab(service, spreadsheet_id, act["worksheet"])
            sheet_events.append({"event": "worksheet_deleted", "worksheet": act["worksheet"]})
            cached_vals = None
        elif t == "rename_tab":
            res = action_rename_tab(service, spreadsheet_id, act["worksheet"], act["new_name"])
            sheet_events.append({"event": "worksheet_renamed", "from": act["worksheet"], "to": act["new_name"]})
            cached_vals = None
        elif t == "set_headers":
            res = action_set_headers(service, spreadsheet_id, act["worksheet"], act["schema"])
            sheet_events.append({"event": "headers_set", "worksheet": act["worksheet"], "schema": act["schema"]})
            cached_vals = None
        elif t == "list_tabs":
            res = action_list_tabs(service, spreadsheet_id)
        elif t == "find_rows":
            res = action_find_rows(service, spreadsheet_id, ws,
                                  where=act.get("where", {}),
                                  limit=act.get("limit"),
                                  offset=int(act.get("offset", 0)),
                                  cached_vals=tab_cache)
        elif t == "edit_where":
            res = action_edit_where(service, spreadsheet_id, ws, col_map,
                                   values=act.get("values", {}),
                                   where=act.get("where", {}),
                                   header_count=header_count,
                                   limit=act.get("limit"),
                                   offset=int(act.get("offset", 0)),
                                   include_snapshots=include_snapshots,
                                   cached_vals=tab_cache)
            cached_vals = None
        elif t == "delete_where":
            res = action_delete_where(service, spreadsheet_id, ws,
                                     where=act.get("where", {}),
                                     header_count=header_count,
                                     limit=act.get("limit"),
                                     offset=int(act.get("offset", 0)),
                                     include_snapshots=include_snapshots,
                                     cached_vals=tab_cache)
            cached_vals = None
        elif t == "get_headers":
            res = action_get_headers(service, spreadsheet_id, ws, cached_vals=tab_cache)
        elif t == "get_tab":
            res = action_get_tab(service, spreadsheet_id, ws,
                                limit=act.get("limit"),
                                offset=int(act.get("offset", 0)),
                                as_objects=bool(act.get("as_objects", False)),
                                cached_vals=tab_cache)
        elif t == "find_first_empty":
            res = action_find_first_empty(service, spreadsheet_id, ws,
                                         header=act.get("header"),
                                         start_row=int(act.get("start_row", 2)),
                                         cached_vals=tab_cache)
        elif t == "swap_rows":
            res = action_swap_rows(service, spreadsheet_id, ws,
                                   row_a=int(act.get("row_a", 0)),
                                   row_b=int(act.get("row_b", 0)),
                                   cached_vals=tab_cache)
            cached_vals = None
        else:
            res = ActionResult(t, None, "failed", f"Unbekannter Typ '{t}'")

        if res is not None:
            results.append(res.to_dict())

    # ── Status zusammenbauen (Prio 2: summarize_tabs nur bei Guidance) ─────────
    policy     = _decide_guidance_policy(results, diagnostics, request_guidance, False)
    tabs_final = summarize_tabs(service, spreadsheet_id) if (request_guidance or policy["include_guidance"]) else tabs_now
    guidance   = build_guidance(
        selected_project, sheet_url, worksheet_name, headers,
        diagnostics, sheet_events,
        tabs=tabs_final, flags=context["flags"],
        service_account_email=_service_account_email(),
    )
    return construct_status(
        project=selected_project,
        update_id=payload.get("update_id"),
        sheet_url=sheet_url,
        results=results, diagnostics=diagnostics, guidance=guidance,
        tabs=tabs_final, sheet_events=sheet_events,
        spreadsheet_title=spreadsheet_title,
        context=context,
        include_guidance=policy["include_guidance"],
    )
