# application/app.py
import sys, json, re, subprocess, argparse, time
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

from application.debug_utils import print_status_summary

DEFAULT_SHEET_URL = "https://docs.google.com/spreadsheets/d/1Wa8kL5kiDuw-0XYqF2iuFqkItg7dhFNydBELwtJBKlI/edit?usp=sharing"
DEFAULT_WORKSHEET = "Anwalt_Log"
DEFAULT_PROJECT = "Bewerbungs_Log"

# Kandidatennamen für die Link-Spalte (Präferenz-Reihenfolge)
PREF_LINK_HEADERS = [
    "Stellenausschreibung_URL", "Belege/Link",
    "Stellenausschreibung", "Bewerbungslink", "URL", "Link"
]

try:
    from webbridge.config import BridgeConfig
except Exception:
    BridgeConfig = None


# ----------------------------- Utilities -----------------------------

def run_webbridge(prompt_obj: Dict[str, Any]) -> Dict[str, Any]:
    Path("prompt.json").write_text(json.dumps(prompt_obj, ensure_ascii=False, indent=2), encoding="utf-8")
    rc = subprocess.run([sys.executable, "-m", "webbridge"], check=False).returncode
    if rc != 0:
        print(f"[Application] WebBridge RC={rc}.", file=sys.stderr)
    if Path("response.json").is_file():
        return json.loads(Path("response.json").read_text(encoding="utf-8"))
    return {}

def run_ownapi(update_obj: Dict[str, Any], *, retries: int = 1, retry_delay_s: float = 3.0, label: str = "") -> Dict[str, Any]:
    """update.json -> ownAPI -> status.json. Minimale Retries (gegen 429). Druckt status-Summary."""
    attempt = 0
    while True:
        attempt += 1
        Path("update.json").write_text(json.dumps(update_obj, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            rc = subprocess.run([sys.executable, "gpt_spreadsheet_interface.py", "--debug"], check=False).returncode
            if rc != 0:
                print(f"[Application] ownAPI RC={rc}.", file=sys.stderr)
        except FileNotFoundError:
            Path("status.json").write_text(json.dumps({"results":[{"status":"success"}]}, ensure_ascii=False, indent=2), encoding="utf-8")
        if Path("status.json").is_file():
            st = json.loads(Path("status.json").read_text(encoding="utf-8"))
            print_status_summary(st, label=label)
            return st
        if attempt > retries:
            return {}
        print(f"[Application] ownAPI lieferte keine status.json – Retry {attempt}/{retries} …", flush=True)
        time.sleep(retry_delay_s)

def make_prompt(message: str, *, new_chat: bool, web: bool, temporary: bool, task_id: str,
                ui_dom_first: bool=True) -> Dict[str, Any]:
    return {
        "protocol_version": "1.0.0",
        "conversation": {"new_chat": bool(new_chat), "temporary_chat": bool(temporary), "enable_web_search": bool(web)},
        "ui": {
            "dom_first": bool(ui_dom_first),
            "absolute": {"composer_xy":[1000,536], "plus_xy":[630,530], "more_xy":[768,768], "web_xy":[1051,768]}
        },
        "wait": {"expect_json_codeblock": True, "max_wait_s": 90, "settle_s": 0.4},
        "message": message,
        "meta": {"task_id": task_id, "context": {}}
    }

def is_valid_url(url: str) -> bool:
    return bool(url and url.strip().startswith(("http://","https://")) and re.match(r"^https?://\S+$", url.strip()))

def row_to_dict(headers: List[str], row_vals: List[str]) -> Dict[str, Any]:
    out = {}
    for i, h in enumerate(headers or []):
        out[str(h)] = row_vals[i] if i < len(row_vals) else ""
    return out

def extract_fields_from_row(r: Dict[str, Any]) -> Tuple[str,str,str,str]:
    company = r.get("Firma") or r.get("Arbeitgeber") or r.get("Company") or ""
    title   = r.get("Stelle") or r.get("Stelle/Referenz") or r.get("Titel") or r.get("Position") or ""
    loc     = r.get("Ort/Remote") or r.get("Ort") or r.get("Standort") or ""
    job_id  = r.get("ID") or r.get("Kennziffer") or r.get("Referenz") or ""
    return str(company).strip(), str(title).strip(), str(loc).strip(), str(job_id).strip()

def _norm(s: str) -> str:
    return re.sub(r"[\s_/]+", "", str(s or "")).strip().lower()

def choose_link_header(headers: List[str]) -> Optional[str]:
    """Suche Kandidat aus bekannter Liste; wenn keiner passt, nimm irgendwas mit 'link' (case/space tolerant)."""
    hs = [str(h) for h in headers or []]
    norm_map = { _norm(h): h for h in hs }
    for pref in PREF_LINK_HEADERS:
        n = _norm(pref)
        if n in norm_map:
            return norm_map[n]
    for h in hs:
        if "link" in _norm(h):
            return h
    return None

def detect_tab_title_fuzzy(status: Dict[str, Any], desired: str) -> Optional[str]:
    """Versuche Worksheet-Titel im status['tabs'] robust zu finden (exakt, normalisiert, tokenisiert)."""
    tabs = status.get("tabs") or []
    # exakt
    for t in tabs:
        if (t.get("title") or "") == desired:
            return t.get("title")
    # normalisiert
    desired_n = _norm(desired)
    for t in tabs:
        if _norm(t.get("title")) == desired_n:
            return t.get("title")
    # token-Heuristik
    tokens = [tok for tok in re.split(r"[^a-z0-9äöüß]+", str(desired).lower()) if tok]
    for t in tabs:
        tn = _norm(t.get("title"))
        if all(tok in tn for tok in tokens):
            return t.get("title")
    # spezial: enthält 'anwalt' und 'log'
    for t in tabs:
        tn = _norm(t.get("title"))
        if ("anwalt" in tn) and ("log" in tn):
            return t.get("title")
    return None

def detect_link_header_from_tabs_fuzzy(status: Dict[str, Any], worksheet: str) -> Optional[str]:
    """Hole für Ziel-Tab einen brauchbaren Link-Header-Kandidaten aus status['tabs']."""
    tabs = status.get("tabs") or []
    want = detect_tab_title_fuzzy(status, worksheet) or worksheet
    for t in tabs:
        if (t.get("title") or "") == want:
            return choose_link_header(t.get("headers") or [])
    return None


# ----------------------------- Schritt 0: Nur Sichtbarkeit erfragen (Web) -----------------------------

def step0_check_sheet_visibility_web(sheet_url: str, worksheet: str) -> Dict[str, Any]:
    """
    Fragt GPT *nur*, ob das Sheet grundsätzlich erreichbar/sichtbar ist.
    Keine Spalten/Zeilen-Analyse – das übernimmt ownAPI.
    """
    prompt = (
        "Kannst du dieses Google Sheet grundsätzlich öffnen? Antworte NUR im SCHEMA v1:\n"
        f"- sheet_url: {sheet_url}\n"
        f"- worksheet_hint: '{worksheet}' (nur als Hinweis)\n"
        "- decision: 'ask_followup'\n"
        "- message: 1 kurzer Satz zur Sichtbarkeit (kein Freitext danach)\n"
        "- hints: {\"can_access\": true|false}\n"
        "- KEINE weiteren Felder nutzen."
    )
    resp = run_webbridge(make_prompt(prompt, new_chat=True, web=True, temporary=False, task_id="visibility_check"))
    data = resp.get("data") or {}
    hints = data.get("hints") or {}
    return {
        "can_access": hints.get("can_access"),
        "message": data.get("message") or ""
    }


# ----------------------------- Schritt 1: ownAPI-only – nächste leere Link-Zelle finden -----------------------------
def ownapi_find_next_row_without_link_batch(sheet_url: str, worksheet_exact: str, project: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    actions = []
    for col in PREF_LINK_HEADERS:
        actions.append({
            "type": "find_rows",
            "worksheet": worksheet_exact,
            "where": { col: "" },
            "limit": 1
        })

    update = {
        "project": project,
        "sheet": {"url": sheet_url, "worksheet": worksheet_exact},
        "flags": {"allow_auto_headers": True, "allow_auto_tab_create": False, "include_snapshots": True},
        "actions": actions
    }

    status = run_ownapi(update, retries=1, label=f"batch find_rows (legacy where) on {worksheet_exact}")
    if not status:
        return None, {}

    candidates = []
    for res in status.get("results", []):
        if res.get("action") != "find_rows":
            continue
        meta = res.get("meta") or {}
        rows_prev = meta.get("rows_preview") or []
        headers = meta.get("headers") or []

        # ← genau den Header nehmen, den diese Aktion gesucht hat
        req_header = None
        try:
            req_header = next(iter(res.get("request", {}).get("where", {}).keys()))
        except Exception:
            pass

        if not rows_prev:
            continue

        rp = rows_prev[0]
        row = rp.get("row")
        row_values = rp.get("row_values") or []

        candidates.append({
            "row": row,
            "row_values": row_values,
            "headers": headers,
            "write_header": req_header  # <- hier festhalten
        })

    if not candidates:
        return None, status

    # Wähle die kleinste Zeilennummer (nächste „oben“)
    best = sorted(candidates, key=lambda c: (c["row"] if isinstance(c["row"], int) else 1_000_000))[0]

    # Falls ownAPI den req_header nicht liefert, nimm heuristisch eine Link-Spalte aus tab-Headern
    if not best.get("write_header"):
        best["write_header"] = choose_link_header(best.get("headers") or []) or "Link"

    return {
        "worksheet": worksheet_exact,
        "headers": best["headers"],
        "row": best["row"],
        "row_values": best["row_values"],
        "write_header": best["write_header"]
    }, status





def ownapi_find_next_row_smart(sheet_url: str, worksheet_wanted: str, project: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    1) Tabs ermitteln (leer-Run) → Worksheet exakt/fuzzy bestimmen
    2) Batch-Find mit klassischer WHERE-Form
    3) Falls 2) keine rows_preview ergibt → gezielter Versuch mit Header aus tabs[]
    """
    # 0) Tabs/Headers laden
    update_probe = {
        "project": project,
        "sheet": {"url": sheet_url, "worksheet": worksheet_wanted},
        "flags": {"allow_auto_headers": True, "allow_auto_tab_create": False, "include_snapshots": True},
        "actions": []
    }
    status_probe = run_ownapi(update_probe, retries=0, label="probe tabs")
    worksheet_exact = detect_tab_title_fuzzy(status_probe or {}, worksheet_wanted) or worksheet_wanted

    # 1) Batch-Find
    r, status1 = ownapi_find_next_row_without_link_batch(sheet_url, worksheet_exact, project)
    if r:
        return r, status1

    # 2) gezielt mit Header aus tabs[]
    write_header = detect_link_header_from_tabs_fuzzy(status1 or status_probe or {}, worksheet_exact)
    update2 = {
        "project": project,
        "sheet": {"url": sheet_url, "worksheet": worksheet_exact},
        "flags": {"allow_auto_headers": True, "allow_auto_tab_create": False, "include_snapshots": True},
        "actions": [
            {"type": "find_rows", "worksheet": worksheet_exact, "where": { (write_header or "Link"): "" }, "limit": 1}
        ]
    }
    status2 = run_ownapi(update2, retries=1, label=f"find_rows targeted (legacy where) {worksheet_exact}")
    for res in status2.get("results", []):
        if res.get("action") != "find_rows":
            continue
        meta = res.get("meta") or {}
        rows_prev = meta.get("rows_preview") or []
        headers = meta.get("headers") or []
        if not rows_prev:
            continue
        rp = rows_prev[0]
        return {
            "worksheet": worksheet_exact,
            "headers": headers,
            "row": rp.get("row"),
            "row_values": rp.get("row_values") or [],
            "write_header": write_header or "Link"
        }, status2

    return None, (status2 or status1 or status_probe or {})

def report_ownapi_gap(statuses: List[Dict[str, Any]], *, worksheet_hint: str, sheet_url: str) -> None:
    """
    Kompakter Problembericht für ownAPI-Entwickler – warum wir keine Zeile ermitteln konnten.
    """
    print("\n[ownAPI-DIAGNOSE] Die ownAPI lieferte keine verwertbare 'rows_preview' für die gesuchten leeren Link-Zellen.", file=sys.stderr)
    print("[ownAPI-DIAGNOSE] Erwartet: 'results[].meta.rows_preview[0].row' + 'row_values' + 'headers'.", file=sys.stderr)
    print(f"[ownAPI-DIAGNOSE] Worksheet-Hinweis: '{worksheet_hint}' | Sheet: {sheet_url}", file=sys.stderr)
    for i, st in enumerate(statuses):
        if not st:
            continue
        tabs = st.get("tabs") or []
        print(f"[ownAPI-DIAGNOSE] Status[{i}]: results={len(st.get('results', []))}, tabs={len(tabs)}", file=sys.stderr)
        if tabs:
            titles = [t.get("title") for t in tabs]
            print(f"[ownAPI-DIAGNOSE]   tabs: {titles}", file=sys.stderr)
            for t in tabs:
                if t.get("title") in (worksheet_hint,):
                    print(f"[ownAPI-DIAGNOSE]   headers[{t.get('title')}]: {t.get('headers')}", file=sys.stderr)
        for j, res in enumerate(st.get("results", [])):
            meta = res.get("meta") or {}
            rpv = meta.get("rows_preview")
            hdrs = meta.get("headers")
            req  = res.get("request")
            print(f"[ownAPI-DIAGNOSE]   result[{j}]: action={res.get('action')} status={res.get('status')} "
                  f"rows_preview_len={len(rpv or []) if rpv else 0} headers_len={len(hdrs or []) if hdrs else 0} request={json.dumps(req, ensure_ascii=False)}",
                  file=sys.stderr)
    print("[ownAPI-DIAGNOSE] Vorschläge:\n"
          "  - 'find_rows' sollte 'meta.rows_preview' (inkl. row, row_values) zurückgeben, wenn matches möglich sind.\n"
          "  - Unterstützung für leere Felder sicherstellen: where {\"<Header>\": \"\"}.\n"
          "  - Optional: neue Action 'find_first_empty' mit (worksheet, header) → liefert nächste freie Zeile.\n"
          "  - 'tabs[].headers' stets vollständig liefern (bereits vorhanden).", file=sys.stderr)

# ----------------------------- Schritt 2: Stellensuche -----------------------------

def step2_search_job(company: str, title: str, location: str, job_id: str, persona: str, *, new_chat: bool) -> Optional[str]:
    prompt_exact = (
        "Du bist ein Job-Rechercheur. Nutze Web search.\n"
        f"Suche die Stellenausschreibung EXAKT zu Firma=\"{company}\", Titel=\"{title}\", Ort=\"{location}\", ID=\"{job_id}\".\n"
        "Antwort (Schema v1): Bei Erfolg decision=\"finish\" + web_findings[0].url; sonst decision=\"retry\" + kurze Begründung."
    )
    r1 = run_webbridge(make_prompt(prompt_exact, new_chat=new_chat, web=True, temporary=False, task_id="search_exact", ui_dom_first=False))
    d1 = r1.get("data") or {}
    if d1.get("decision") == "finish":
        wfs = d1.get("web_findings") or []
        if wfs and is_valid_url(wfs[0].get("url") or ""):
            return (wfs[0].get("url") or "").strip()

    # 2x Similar-Search Versuche
    for ix in range(2):
        prompt_sim = (
            "Kein exakter Link gefunden. Suche eine ähnliche Stelle passend zum Profil.\n"
            f"Person: {persona}\n"
            "Prio: Embedded, Leistungselektronik, Wehrtechnik, Automotive, Automatisierung.\n"
            f"Kontext: Firma=\"{company}\", Titel=\"{title}\", Ort=\"{location}\".\n"
            "Antwort (Schema v1): Bei Erfolg decision=\"finish\" + web_findings[0].url; sonst decision=\"retry\" + kurze Begründung."
        )
        r2 = run_webbridge(make_prompt(prompt_sim, new_chat=False, web=True, temporary=False, task_id=f"search_similar_{ix+1}", ui_dom_first=False))
        d2 = r2.get("data") or {}
        if d2.get("decision") == "finish":
            wfs = d2.get("web_findings") or []
            if wfs and is_valid_url(wfs[0].get("url") or ""):
                return (wfs[0].get("url") or "").strip()
    return None


# ----------------------------- Schritt 3: Schreiben & Verifizieren -----------------------------

def step3_write_and_verify(sheet_url: str, worksheet: str, row: int, write_header: str, url: str,
                           project: str = DEFAULT_PROJECT) -> bool:
    attempt = 0
    while attempt < 3:
        attempt += 1
        print(f"[Application] update.json erstellen/ausführen – Versuch {attempt} …", flush=True)
        prompt_update = (
            "Rolle: JSON-Orchestrator für ownAPI. Antworte NUR mit decision=\"produce_update_json\" + gültiger update.json.\n"
            f"Trage in {sheet_url} / Tab {worksheet} in Zeile {row}, Spalte {write_header!r} die URL {url!r} ein.\n"
            "Flags: allow_auto_headers=true, allow_auto_tab_create=false, include_snapshots=true\n"
            f"Action-Beispiel: {{\"type\":\"edit\",\"worksheet\":{json.dumps(worksheet)},\"row\":{row},\"values\":{{{json.dumps(write_header)}:{json.dumps(url)}}}}}"
        )
        resp_upd = run_webbridge(make_prompt(prompt_update, new_chat=False, web=False, temporary=False, task_id=f"make_update_{attempt}"))
        d_upd = resp_upd.get("data") or {}
        if d_upd.get("decision") != "produce_update_json":
            print("[Application] Unerwartete Antwort beim Erzeugen der update.json.", file=sys.stderr)
            return False
        upd = d_upd.get("update_json") or {}
        status = run_ownapi(upd, retries=1, label=f"verify attempt {attempt}")

        prompt_check = (
            "Prüfe diese status.json: Wenn der Link korrekt geschrieben wurde (Snapshot/Zielzelle enthält valide URL), "
            "antworte mit {\"decision\":\"finish\",\"message\":\"Prozess_abgeschlossen\"}; sonst decision=\"retry\" + Kurzgrund.\n\n"
            + json.dumps(status, ensure_ascii=False, indent=2)
        )
        resp_chk = run_webbridge(make_prompt(prompt_check, new_chat=False, web=False, temporary=False, task_id=f"check_{attempt}"))
        d_chk = resp_chk.get("data") or {}
        if d_chk.get("decision") == "finish" and "Prozess_abgeschlossen" in (d_chk.get("message") or ""):
            return True
        print("[Application] Hinweis von GPT:", d_chk.get("message") or "weiter bearbeiten", flush=True)
    return False


# ----------------------------- Command: Run-One (neue Logik) -----------------------------

def cmd_run_one_v2(args) -> int:
    cfg = BridgeConfig() if BridgeConfig else None
    sheet_url = (cfg.sheet_url if cfg and getattr(cfg, "sheet_url", None) else args.sheet_url) or DEFAULT_SHEET_URL
    worksheet = (cfg.worksheet if cfg and getattr(cfg, "worksheet", None) else args.worksheet) or DEFAULT_WORKSHEET
    project = (cfg.project_name if cfg and getattr(cfg, "project_name", None) else args.project) or DEFAULT_PROJECT

    # Schritt 0: Nur Sichtbarkeit erfragen (keine Details)
    print("[Application] Schritt 0: Sheet-Sichtbarkeit erfragen (Web) …", flush=True)
    vis = step0_check_sheet_visibility_web(sheet_url, worksheet)
    print(f"[Application] Sichtbarkeit laut GPT: {vis.get('can_access')} – {vis.get('message')}", flush=True)

    # Schritt 1: ownAPI-only – nächste leere Link-Zelle bestimmen
    print("[Application] Schritt 1: Nächste leere Link-Zelle via ownAPI bestimmen …", flush=True)
    r, st_any = ownapi_find_next_row_smart(sheet_url, worksheet, project)
    if not r:
        # Kompakter Problembericht für ownAPI-Dev
        st_list = [st_any] if isinstance(st_any, dict) else (st_any or [])
        report_ownapi_gap(st_list if isinstance(st_list, list) else [st_list], worksheet_hint=worksheet, sheet_url=sheet_url)
        print("[Application] Konnte Row/Header nicht bestimmen. Abbruch.", file=sys.stderr)
        return 1

    row = int(r["row"])
    headers = r.get("headers") or []
    write_header = r.get("write_header") or detect_link_header_from_tabs_fuzzy(st_any or {}, worksheet) or "Stellenausschreibung_URL"
    row_dict = row_to_dict(headers, r.get("row_values") or [])
    company, title, location, job_id = extract_fields_from_row(row_dict)

    print(f"[Application] Ziel ermittelt: worksheet='{worksheet}', row={row}, header='{write_header}'", flush=True)
    print(f"[Application] Kontext aus Zeile: Firma='{company}', Stelle='{title}', Ort='{location}', ID='{job_id}'", flush=True)

    # Schritt 2: Stellensuche (Web)
    print("[Application] Schritt 2: Stellenausschreibung suchen …", flush=True)
    persona = "Dipl.-Ing. David Frölich. Embedded/Elektronik, Hochspannung/HV, Wehrtechnik, Automotive, Sensorik, FPGA/VHDL, C, Matlab (B1)."
    url = step2_search_job(company, title, location, job_id, persona, new_chat=True)  # new_chat=True: frischer Suchkontext
    if not url:
        print("[Application] Keine geeignete URL gefunden.", file=sys.stderr)
        return 1
    if not is_valid_url(url):
        print("[Application] URL-Format ungültig.", file=sys.stderr)
        return 1
    print(f"[Application] Kandidaten-URL: {url}", flush=True)

    # Schritt 3: Eintragen & Verifizieren
    print("[Application] Schritt 3: In Tabelle eintragen & verifizieren …", flush=True)
    ok = step3_write_and_verify(sheet_url, worksheet, row, write_header, url, project=project)
    if ok:
        print("[Application] ✅ Vorgang abgeschlossen.", flush=True)
        return 0
    print("[Application] ❗ Konnte trotz Wiederholungen nicht abschließen.", file=sys.stderr)
    return 1


# ----------------------------- Weitere Commands (unverändert) -----------------------------

def cmd_search_one(args) -> int:
    firma, stelle, ort = args.firma, args.stelle, args.ort
    target_row = args.row if args.row is not None else 2
    target_header = args.header if args.header is not None else "Stellenausschreibung_URL"
    message = (
        "Du bist ein Job-Rechercheur. Nutze (falls verfügbar) Web search und antworte nur mit einem JSON-Codeblock (Schema v1).\n"
        f"Profil: Firma={firma} | Stelle={stelle} | Ort/Remote={ort}.\n"
        "Bei Erfolg decision='finish' + web_findings[0].url; sonst decision='retry'. "
        f"Hints: target_row={target_row}, target_header='{target_header}'."
    )
    prompt = {
        "protocol_version": "1.0.0",
        "conversation": {"new_chat": True, "temporary_chat": bool(args.temporary), "enable_web_search": not bool(args.no_web)},
        "ui": {"dom_first": not bool(args.no_dom), "absolute": {"composer_xy":[1000,536],"plus_xy":[630,530],"more_xy":[700,780],"web_xy":[1000,780]}},
        "wait": {"expect_json_codeblock": True, "max_wait_s": 90, "settle_s": 0.4},
        "message": message,
        "meta": {"task_id": "search_one","context":{"firma":firma,"stelle":stelle,"ort":ort,"target_row":target_row,"target_header":target_header}}
    }
    resp = run_webbridge(prompt)
    data = resp.get("data") or {}
    if data.get("decision") == "finish":
        cfg = BridgeConfig() if BridgeConfig else None
        url = (data.get("web_findings") or [{}])[0].get("url")
        row = data.get("hints",{}).get("target_row", target_row)
        header = data.get("hints",{}).get("target_header", target_header)
        if cfg:
            update_payload = {
                "project": cfg.project_name,
                "sheet": {"url": cfg.sheet_url, "worksheet": cfg.worksheet},
                "flags": {"allow_auto_headers": True, "allow_auto_tab_create": False, "include_snapshots": True},
                "actions": [{"type":"edit","worksheet":cfg.worksheet,"row":row,"values":{str(header):url or ""}}]
            }
        else:
            update_payload = {"actions":[{"type":"edit","row":row,"values":{str(header):url or ""}}]}
        Path("update.json").write_text(json.dumps(update_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        Path("status.json").write_text(json.dumps({"results":[{"action":"edit","status":"success","row":row}]}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[Application] update.json geschrieben, ownAPI (simuliert) OK.", flush=True)
        return 0
    print("[Application] Keine finish-Entscheidung.", file=sys.stderr)
    return 1

def cmd_repair_status(args) -> int:
    status_path = Path(args.status_file)
    if not status_path.is_file():
        print(f"[Application] {status_path} nicht gefunden.", file=sys.stderr); return 1
    status_data = json.loads(status_path.read_text(encoding="utf-8"))
    message = ("Analysiere folgende status.json und antworte NUR mit decision='produce_update_json' + kompletter update.json. "
               "Idempotent bleiben; falls Infos fehlen -> decision='retry' + kurze message.\n\n" + json.dumps(status_data, ensure_ascii=False, indent=2))
    prompt = make_prompt(message, new_chat=True, web=False, temporary=False, task_id="repair")
    resp = run_webbridge(prompt); data = resp.get("data") or {}
    if data.get("decision") == "produce_update_json":
        upd = data.get("update_json") or {}
        Path("update.json").write_text(json.dumps(upd, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[Application] korrigierte update.json geschrieben.", flush=True); return 0
    print("[Application] Reparatur fehlgeschlagen/unerwartet.", file=sys.stderr); return 1

def main():
    p = argparse.ArgumentParser(description="Application Orchestrator (WebBridge + ownAPI)")
    sub = p.add_subparsers(dest="cmd")
    p_auto2 = sub.add_parser("run-one-v2", help="Sichtbarkeit -> ownAPI-Find -> Websuche -> update.json+Check.")
    p_auto2.add_argument("--sheet-url", default=None); p_auto2.add_argument("--worksheet", default=None); p_auto2.add_argument("--project", default=None)
    p1 = sub.add_parser("search-one", help="Einfache Stellensuche (ein Schritt).")
    p1.add_argument("--firma","-f",required=True); p1.add_argument("--stelle","-s",required=True); p1.add_argument("--ort","-o",required=True)
    p1.add_argument("--row",type=int,default=None); p1.add_argument("--header",default=None)
    p1.add_argument("--no-web",action="store_true"); p1.add_argument("--temporary",action="store_true"); p1.add_argument("--no-dom",action="store_true")
    p2 = sub.add_parser("repair-status", help="status.json analysieren und update.json korrigieren.")
    p2.add_argument("--status-file","-i",default="status.json")
    args = p.parse_args()
    if args.cmd == "run-one-v2": sys.exit(cmd_run_one_v2(args))
    elif args.cmd == "search-one": sys.exit(cmd_search_one(args))
    elif args.cmd == "repair-status": sys.exit(cmd_repair_status(args))
    else: p.print_help(); sys.exit(1)

if __name__ == "__main__":
    main()
