from __future__ import annotations
import os, re, logging, threading, time
from typing import Any, List, Optional, Dict

try:
    from google.oauth2.service_account import Credentials  # type: ignore
    from googleapiclient.discovery import build  # type: ignore
    from googleapiclient.errors import HttpError  # type: ignore
except ImportError:
    Credentials = None  # type: ignore
    build = None  # type: ignore
    HttpError = Exception  # type: ignore

LOG = logging.getLogger("gpt_sheet")

# ---------- Thread-lokaler Service-Cache ----------
# Jeder Flask-Thread bekommt seinen eigenen Service, damit die
# nicht-thread-safe googleapiclient-Objekte nicht geteilt werden.
_thread_local = threading.local()

def get_cached_service():
    """Gibt thread-lokalen Google Sheets Service zurück."""
    if not hasattr(_thread_local, "service"):
        _thread_local.service = None
    if _thread_local.service is None:
        creds = load_credentials()
        if creds:
            _thread_local.service = build_service(creds)
    return _thread_local.service

def invalidate_service_cache():
    if hasattr(_thread_local, "service"):
        _thread_local.service = None

# ---------- Retry-Wrapper ----------
def execute_with_retry(req, max_retries: int = 3):
    """Führt einen Google API Request aus; wiederholt bei 429/500/503 mit Exponential Backoff."""
    delay = 1.0
    for attempt in range(max_retries + 1):
        try:
            return req.execute()
        except HttpError as exc:
            code = int(exc.resp.status) if hasattr(exc, "resp") else 0
            if attempt < max_retries and code in (429, 500, 503):
                LOG.warning("Google API %d – retry %d/%d in %.1fs", code, attempt + 1, max_retries, delay)
                time.sleep(delay)
                delay *= 2
            else:
                raise

# ---------- Auth / Service ----------
def load_credentials() -> Optional[Any]:
    if Credentials is None:
        LOG.warning("Google API libraries are not installed; actions requiring Sheets access will fail")
        return None
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path and os.path.isfile(cred_path):
        try:
            creds = Credentials.from_service_account_file(
                cred_path,
                scopes=[
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive",
                    "https://www.googleapis.com/auth/drive.file",
                ],
            )
            LOG.debug(f"Loaded credentials from env path: {cred_path}")
            return creds
        except Exception as exc:
            LOG.error(f"Failed to load credentials from {cred_path}: {exc}")
    local_path = os.path.join(os.getcwd(), "service_account.json")
    if os.path.isfile(local_path):
        try:
            creds = Credentials.from_service_account_file(
                local_path,
                scopes=[
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive",
                    "https://www.googleapis.com/auth/drive.file",
                ],
            )
            LOG.debug("Loaded credentials from ./service_account.json")
            return creds
        except Exception as exc:
            LOG.error(f"Failed to load credentials from {local_path}: {exc}")
    return None

def build_service(creds: Any):
    LOG.debug("Initialising Sheets service …")
    return build("sheets", "v4", credentials=creds)

# ---------- Utils ----------
def extract_spreadsheet_id(url: str) -> Optional[str]:
    m = re.search(r"/d/([a-zA-Z0-9-_]+)", url)
    return m.group(1) if m else None

def get_sheet_meta(service: Any, spreadsheet_id: str) -> Dict[str, Any]:
    return service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(title,sheetId,index)),properties(title)"
    ).execute()

def get_sheet_id(service: Any, spreadsheet_id: str, worksheet_name: str) -> Optional[int]:
    try:
        LOG.debug("Requesting spreadsheet metadata for sheetId lookup…")
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets(properties(title,sheetId,index))"
        ).execute()
    except HttpError as exc:
        LOG.error(f"Failed to retrieve spreadsheet metadata: {exc}")
        return None
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == worksheet_name:
            LOG.debug(f"Resolved sheetId for '{worksheet_name}': {props.get('sheetId')}")
            return props.get("sheetId")
    return None

def list_sheets(service: Any, spreadsheet_id: str) -> List[Dict[str, Any]]:
    """Return list of sheets: [{title, sheetId, index}, ...]."""
    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets(properties(title,sheetId,index))"
        ).execute()
        out = []
        for s in meta.get("sheets", []):
            p = s.get("properties", {})
            out.append({"title": p.get("title"), "sheetId": p.get("sheetId"), "index": p.get("index")})
        return out
    except HttpError as exc:
        LOG.error(f"Failed to list sheets: {exc}")
        return []

def get_tab_values(service: Any, spreadsheet_id: str, worksheet: str) -> List[List[str]]:
    """Liest den gesamten Tab (inkl. Headerzeile). Erste Zeile = Header."""
    try:
        resp = execute_with_retry(
            service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=worksheet
            )
        )
        return resp.get("values", [])
    except Exception:
        return []

def get_header_row(service: Any, spreadsheet_id: str, worksheet_name: str) -> List[str]:
    try:
        LOG.debug(f"Reading header row from {worksheet_name}!1:1 …")
        result = execute_with_retry(
            service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=f"{worksheet_name}!1:1"
            )
        )
        values = result.get("values", [])
        return values[0] if values else []
    except HttpError as exc:
        LOG.error(f"Failed to retrieve header row: {exc}")
        return []

def build_column_mapping(headers: List[str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for idx, header in enumerate(headers):
        n = idx
        letters = ""
        while True:
            n, r = divmod(n, 26)
            letters = chr(ord('A') + r) + letters
            if n == 0:
                break
            n -= 1
        mapping[header] = letters
    return mapping

def parse_updated_row_from_range(range_str: str) -> Optional[int]:
    a1 = range_str.split("!", 1)[1] if "!" in range_str else range_str
    start_cell = a1.split(":")[0]
    import re as _re
    m = _re.search(r"(\d+)", start_cell)
    return int(m.group(1)) if m else None

# ---------- Worksheet management ----------
def add_sheet(service: Any, spreadsheet_id: str, title: str, index: Optional[int] = None) -> int:
    req = {"addSheet": {"properties": {"title": title}}}
    if index is not None:
        req["addSheet"]["properties"]["index"] = int(index)
    try:
        resp = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": [req]}
        ).execute()
        sid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
        LOG.info(f"Worksheet '{title}' angelegt (sheetId={sid})")
        return sid
    except HttpError as exc:
        LOG.error(f"add_sheet failed: {exc}")
        raise

def delete_sheet(service: Any, spreadsheet_id: str, sheet_id: int) -> None:
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"deleteSheet": {"sheetId": sheet_id}}]},
        ).execute()
        LOG.info(f"Worksheet gelöscht (sheetId={sheet_id})")
    except HttpError as exc:
        LOG.error(f"delete_sheet failed: {exc}")
        raise

def rename_sheet(service: Any, spreadsheet_id: str, sheet_id: int, new_title: str) -> None:
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "title": new_title},
                    "fields": "title"
                }
            }]},
        ).execute()
        LOG.info(f"Worksheet umbenannt zu '{new_title}' (sheetId={sheet_id})")
    except HttpError as exc:
        LOG.error(f"rename_sheet failed: {exc}")
        raise

def ensure_headers(service: Any, spreadsheet_id: str, worksheet_name: str, headers: list[str]) -> None:
    """Write headers into row 1 (overwrite)."""
    if not headers:
        raise ValueError("Leere Headerliste")
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{worksheet_name}!1:1",
        valueInputOption="RAW",
        body={"values": [headers]},
    ).execute()
    LOG.info(f"Header initialisiert: {headers}")



def summarize_tabs(service: Any, spreadsheet_id: str) -> List[Dict[str, Any]]:
    """
    Liefert eine Übersicht aller Tabs inkl. Titel, sheetId, Index,
    Grid-Größe (rows/cols) und Headerzeile (falls vorhanden).
    """
    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(title,sheetId,index,gridProperties(rowCount,columnCount)))"
        ).execute()
    except HttpError as exc:
        LOG.error(f"summarize_tabs failed: {exc}")
        return []

    tabs: List[Dict[str, Any]] = []
    for s in meta.get("sheets", []):
        props = s.get("properties", {}) or {}
        title = props.get("title")
        grid = props.get("gridProperties", {}) or {}
        rows = grid.get("rowCount")
        cols = grid.get("columnCount")

        # Header (erste Zeile) lesen – failsafe
        try:
            headers = get_header_row(service, spreadsheet_id, title) if title else []
        except Exception:
            headers = []

        tabs.append({
            "title": title,
            "sheetId": props.get("sheetId"),
            "index": props.get("index"),
            "rows": rows,
            "cols": cols,
            "headers": headers,
        })
    return tabs
