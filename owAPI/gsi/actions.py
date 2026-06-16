# gsi/actions.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import re
from .sheets import (
    parse_updated_row_from_range, summarize_tabs,
    get_tab_values as _get_tab_values, execute_with_retry,
)

#append — 1 Zeile anhängen (Dictionary → Zellen).
#
#append_rows — mehrere Zeilen in einem Rutsch anhängen.
#
#edit — einzelne Zeile (per Zeilennummer) in bestimmten Spalten ändern.
#
#delete — einzelne Zeile löschen.
#
#comment — wie edit (semantisch als „Anmerkung“ nutzbar).
#
#find_rows — Zeilen filtern (neue WHERE-DSL und Legacy {"Spalte": ""} unterstützt). Liefert headers, matches, rows_preview.
#
#edit_where — alle per WHERE gefilterten Zeilen bearbeiten (Bulk-Edit).
#
#delete_where — alle per WHERE gefilterten Zeilen löschen (Bulk-Delete).
#
#find_first_empty — erste leere Zelle in einer Spalte (per Headername) → gibt row + a1.
#
#get_tab (NEU) — kompletten Tab lesen (mit limit/offset, optional as_objects: true → Dicts je Zeile).
#
#get_headers (NEU) — nur die Headerzeile lesen.
#
#list_tabs (verbessert) — Tab-Namen + (optional) Headercount zurückgeben (nicht nur „Guidance erwähnt“, sondern als Result).


@dataclass
class ActionResult:
    action: str
    row: Optional[int]
    status: str
    error: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None  # für snapshots, ranges usw.

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "action": self.action,
            "status": self.status,
        }
        if self.row is not None:
            d["row"] = self.row
        if self.error:
            d["error"] = self.error
        if self.meta:
            d["meta"] = self.meta
        return d

# --------- Helpers ---------
def action_get_headers(
    service, spreadsheet_id: str, worksheet: str,
    cached_vals: Optional[List[List[str]]] = None,
) -> ActionResult:
    vals = cached_vals if cached_vals is not None else _get_tab_values(service, spreadsheet_id, worksheet)
    headers = vals[0] if vals else []
    return ActionResult("get_headers", 1 if headers else None, "success", None, {"headers": headers})

def action_get_tab(
    service, spreadsheet_id: str, worksheet: str,
    limit: Optional[int] = None, offset: int = 0, as_objects: bool = False,
    cached_vals: Optional[List[List[str]]] = None,
) -> ActionResult:
    vals = cached_vals if cached_vals is not None else _get_tab_values(service, spreadsheet_id, worksheet)
    headers = vals[0] if vals else []
    data = vals[1:] if len(vals) > 1 else []
    if offset: data = data[offset:]
    if isinstance(limit, int) and limit >= 0: data = data[:limit]
    if as_objects and headers:
        rows_out = [ {headers[i]: (r[i] if i < len(r) else "") for i in range(len(headers))} for r in data ]
    else:
        rows_out = data
    meta = {
        "headers": headers,
        "rows": rows_out,
        "row_count": len(vals) - 1 if vals else 0,
        "offset": offset,
        "limit": limit,
        "as_objects": bool(as_objects),
    }
    return ActionResult("get_tab", None, "success", None, meta)

def action_list_tabs(service, spreadsheet_id: str) -> ActionResult:
    tabs = summarize_tabs(service, spreadsheet_id) or []
    # shape: [{"title":"Tab1","sheetId":0,"headers":["..."]}, ...]
    return ActionResult("list_tabs", None, "success", None, {"tabs": tabs})


def action_append_rows(
    service: Any,
    spreadsheet_id: str,
    worksheet: str,
    col_map: Dict[str, str],      # {"Datum":"A", "Firma":"B", ...}
    header_count: int,
    rows: List[Dict[str, Any]],
    include_snapshots: bool = True,   # wird hier nicht genutzt, für Symmetrie
) -> ActionResult:
    if not rows:
        return ActionResult("append_rows", None, "failed", "rows[] ist leer")

    # 1) Validierung & Mapping in Header-Reihenfolge
    header_by_index = [None] * header_count
    # col_map gibt Buchstaben → baue Indexmap (A->0, B->1, …)
    def _col_to_idx(letter: str) -> int:
        n = 0
        for ch in letter:
            n = n * 26 + (ord(ch) - ord('A') + 1)
        return n - 1

    idx_map = {col: _col_to_idx(letter) for col, letter in col_map.items()}
    values_matrix: List[List[Any]] = []
    for r in rows:
        # Fehlende Spalten werden als leere Zellen geschrieben
        row_vals = [""] * header_count
        for k, v in r.items():
            if k not in idx_map:
                return ActionResult("append_rows", None, "failed", f"Spalte '{k}' unbekannt")
            row_vals[idx_map[k]] = v
        values_matrix.append(row_vals)

    try:
        # 2) Einmaliger Append
        rng = f"{worksheet}!A1"   # A1 genügt; Append setzt automatisch an erste freie Zeile
        resp = service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=rng,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": values_matrix},
        ).execute()

        # 3) Metadaten ermitteln
        updates = (resp or {}).get("updates", {})
        updated_range = updates.get("updatedRange") or ""
        first_row = parse_updated_row_from_range(updated_range) if updated_range else None
        count = updates.get("updatedRows") or len(values_matrix)
        meta = {"count": count, "worksheet": worksheet}
        if first_row:
            meta["first_row"] = first_row
            meta["last_row"] = first_row + count - 1
            meta["updated_range"] = updated_range

        return ActionResult("append_rows", None, "success", None, meta)
    except Exception as exc:
        return ActionResult("append_rows", None, "failed", f"API error: {exc}")


def _is_number(x: Any) -> bool:
    try:
        float(str(x).replace(",", "."))
        return True
    except Exception:
        return False

def _to_number(x: Any) -> float:
    return float(str(x).replace(",", "."))

def _compile_where(where: Dict[str, Any]):
    """
    Unterstützt:
      • Neue DSL:
          {"col":"Status","op":"icontains","value":"geplant"}
          {"and":[ {...}, {"or":[...]} ]}
      • Legacy-Map:
          {"Status": "geplant", "Kanal": {"op":"eq","value":"Web"}}
          {"Link": ""}   -> leer
    Operatoren:
      eq, neq, contains, icontains, startswith, endswith, regex,
      in, nin, empty, not_empty, gt, gte, lt, lte
    """
    def normalize_op(op: str) -> str:
        return (op or "eq").strip().lower()

    def make_leaf(col: str, op: str, val: Any):
        op = normalize_op(op)
        # Vorcompilierte Regex?
        rx = None
        if op == "regex":
            try:
                rx = re.compile(str(val))
            except re.error:
                rx = re.compile("^$")  # unmatchable

        def _p(header_idx_map: Dict[str,int], row: List[str]) -> bool:
            if col not in header_idx_map:
                return False
            idx = header_idx_map[col]
            cell = row[idx] if idx < len(row) else ""

            s = str(cell)
            v = "" if val is None else str(val)

            if op == "empty":
                return (cell == "" or cell is None)
            if op in ("not_empty","notempty"):
                return (cell != "" and cell is not None)
            if op == "eq":
                return s == v
            if op == "neq":
                return s != v
            if op == "contains":
                return v in s
            if op == "icontains":
                return v.lower() in s.lower()
            if op == "startswith":
                return s.startswith(v)
            if op == "endswith":
                return s.endswith(v)
            if op == "regex":
                return rx.search(s) is not None
            if op == "in":
                try:
                    it = val if isinstance(val, list) else [val]
                except Exception:
                    it = [val]
                return s in [str(x) for x in it]
            if op == "nin":
                try:
                    it = val if isinstance(val, list) else [val]
                except Exception:
                    it = [val]
                return s not in [str(x) for x in it]
            if op in ("gt","gte","lt","lte"):
                # numerisch, fallback lexikografisch
                if _is_number(s) and _is_number(v):
                    a, b = _to_number(s), _to_number(v)
                else:
                    a, b = s, v
                if op == "gt":  return a >  b
                if op == "gte": return a >= b
                if op == "lt":  return a <  b
                if op == "lte": return a <= b
            return False
        return _p

    def build_pred(node: Dict[str, Any]):
        # Neue DSL?
        if "col" in node:
            return make_leaf(node.get("col"), node.get("op", "eq"), node.get("value"))
        # Legacy-Map (alle Keys → AND)
        parts = []
        for k, v in (node or {}).items():
            if k in ("and","or"):  # wird in compose behandelt
                continue
            if isinstance(v, dict) and ("op" in v or "value" in v):
                parts.append(make_leaf(k, v.get("op", "eq"), v.get("value")))
            else:
                # Leerer String => empty
                op = "empty" if (v is None or v == "") else "eq"
                parts.append(make_leaf(k, op, v))
        if not parts:
            # kein erkennbarer Leaf -> match-all
            return lambda _h,_r: True
        return lambda h,r: all(p(h,r) for p in parts)

    def compose(node: Any):
        if not isinstance(node, dict) or not node:
            return lambda _h,_r: True
        if "and" in node and isinstance(node["and"], list):
            parts = [compose(x) for x in node["and"]]
            return lambda h,r: all(p(h,r) for p in parts)
        if "or" in node and isinstance(node["or"], list):
            parts = [compose(x) for x in node["or"]]
            return lambda h,r: any(p(h,r) for p in parts)
        return build_pred(node)

    return compose(where or {})


def _col_letter(idx0: int) -> str:
    n = idx0
    s = ""
    while True:
        n, r = divmod(n, 26)
        s = chr(ord('A') + r) + s
        if n == 0:
            break
        n -= 1
    return s

def action_swap_rows(
    service: Any,
    spreadsheet_id: str,
    worksheet: str,
    row_a: int,
    row_b: int,
    cached_vals: Optional[List[List[str]]] = None,
) -> ActionResult:
    """Tauscht zwei Datenzeilen atomar in einem einzigen batchUpdate-Call.

    row_a, row_b: 1-basiert im Datenbereich (1 = erste Zeile nach Header).
    """
    if row_a == row_b:
        return ActionResult("swap_rows", None, "failed", "row_a und row_b sind identisch")
    if row_a < 1 or row_b < 1:
        return ActionResult("swap_rows", None, "failed", "row_a und row_b müssen >= 1 sein")

    vals = cached_vals if cached_vals is not None else _get_tab_values(service, spreadsheet_id, worksheet)
    if not vals:
        return ActionResult("swap_rows", None, "failed", "Tab leer oder nicht lesbar")

    # vals[0] = Header, vals[1] = erste Datenzeile (row_a=1), vals[2] = zweite, …
    max_data = len(vals) - 1
    if row_a > max_data:
        return ActionResult("swap_rows", None, "failed", f"row_a={row_a} außerhalb Bereich 1..{max_data}")
    if row_b > max_data:
        return ActionResult("swap_rows", None, "failed", f"row_b={row_b} außerhalb Bereich 1..{max_data}")

    col_count = len(vals[0])
    end_col = _col_letter(max(col_count - 1, 0))

    def _pad(row: List[str]) -> List[str]:
        r = list(row)
        return r + [""] * max(0, col_count - len(r))

    data_a = _pad(vals[row_a])   # row_a=1 → vals[1]
    data_b = _pad(vals[row_b])   # row_b=2 → vals[2]

    # Sheet-Zeilennummern (1-basiert): Header ist Zeile 1, erste Datenzeile ist Zeile 2
    sheet_row_a = row_a + 1
    sheet_row_b = row_b + 1

    all_data = [
        {"range": f"{worksheet}!A{sheet_row_a}:{end_col}{sheet_row_a}", "values": [data_b]},
        {"range": f"{worksheet}!A{sheet_row_b}:{end_col}{sheet_row_b}", "values": [data_a]},
    ]

    try:
        execute_with_retry(
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": all_data},
            )
        )
    except Exception as exc:
        return ActionResult("swap_rows", None, "failed", f"API error: {exc}")

    return ActionResult("swap_rows", None, "success", None, {
        "swapped": [row_a, row_b],
        "sheet_rows": [sheet_row_a, sheet_row_b],
    })


def action_find_first_empty(
    service: Any,
    spreadsheet_id: str,
    worksheet: str,
    header: str,
    start_row: int = 2,
    cached_vals: Optional[List[List[str]]] = None,
) -> "ActionResult":
    vals = cached_vals if cached_vals is not None else _get_tab_values(service, spreadsheet_id, worksheet)
    if not vals:
        return ActionResult("find_first_empty", None, "failed", "Tab leer oder nicht lesbar")

    headers = vals[0]
    try:
        idx = headers.index(header)
    except ValueError:
        return ActionResult("find_first_empty", None, "failed", f"Spalte '{header}' nicht gefunden")

    # ab start_row suchen
    first = None
    for r_idx, row in enumerate(vals[1:], start=2):
        if r_idx < start_row:
            continue
        cell = row[idx] if idx < len(row) else ""
        if cell == "" or cell is None:
            first = r_idx
            break

    meta = {"header": header, "headers": headers}
    if first:
        col_letter = _col_letter(idx)
        meta.update({
            "row": first,
            "a1": f"{worksheet}!{col_letter}{first}",
        })
        return ActionResult("find_first_empty", first, "success", None, meta)

    return ActionResult("find_first_empty", None, "success", None, {**meta, "row": None})

def action_find_rows(
    service: Any,
    spreadsheet_id: str,
    worksheet: str,
    where: Dict[str, Any],
    limit: Optional[int] = None,
    offset: int = 0,
    cached_vals: Optional[List[List[str]]] = None,
) -> ActionResult:
    vals = cached_vals if cached_vals is not None else _get_tab_values(service, spreadsheet_id, worksheet)
    if not vals:
        return ActionResult("find_rows", None, "success", None, {"matches": [], "headers": [], "rows_preview": []})

    headers = vals[0]
    header_idx = {h: i for i, h in enumerate(headers)}
    pred = _compile_where(where)

    matches = []
    for i, row in enumerate(vals[1:], start=2):  # ab Datenzeile
        if pred(header_idx, row):
            matches.append({"row": i, "row_values": row})

    # offset/limit anwenden
    if offset:
        matches = matches[offset:]
    if isinstance(limit, int) and limit >= 0:
        matches = matches[:limit]

    meta = {
        "headers": headers,
        "matches": [m["row"] for m in matches],
        "rows_preview": matches
    }
    # keine einzelne row – wir geben Liste in meta aus
    return ActionResult("find_rows", None, "success", None, meta)

def action_edit_where(
    service: Any,
    spreadsheet_id: str,
    worksheet: str,
    col_map: Dict[str, str],
    values: Dict[str, Any],
    where: Dict[str, Any],
    header_count: int,
    limit: Optional[int] = None,
    offset: int = 0,
    include_snapshots: bool = False,
    cached_vals: Optional[List[List[str]]] = None,
) -> ActionResult:
    vals = cached_vals if cached_vals is not None else _get_tab_values(service, spreadsheet_id, worksheet)
    if not vals:
        return ActionResult("edit_where", None, "failed", "Tab leer oder nicht lesbar")

    headers = vals[0]
    header_idx = {h: i for i, h in enumerate(headers)}
    for c in values.keys():
        if c not in header_idx:
            return ActionResult("edit_where", None, "failed", f"Spalte '{c}' fehlt")

    pred = _compile_where(where)
    targets = []
    for i, row in enumerate(vals[1:], start=2):
        if pred(header_idx, row):
            targets.append(i)

    if offset:
        targets = targets[offset:]
    if isinstance(limit, int) and limit >= 0:
        targets = targets[:limit]

    if not targets:
        return ActionResult("edit_where", None, "success", None, {"edited": 0, "matches": []})

    # Snapshots vor dem Schreiben einlesen (optional)
    snapshots_before: Dict[int, List[str]] = {}
    if include_snapshots:
        for row_num in targets:
            snapshots_before[row_num] = _read_row_snapshot(service, spreadsheet_id, worksheet, row_num, header_count)

    # Alle Zeilen+Spalten in einem einzigen batchUpdate zusammenfassen
    edited_ranges: List[str] = []
    all_data: List[Dict[str, Any]] = []
    for row_num in targets:
        for col_name, val in values.items():
            col_letter = col_map[col_name]
            a1 = f"{worksheet}!{col_letter}{row_num}:{col_letter}{row_num}"
            all_data.append({"range": a1, "values": [[val]]})
            edited_ranges.append(a1)

    try:
        execute_with_retry(
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": all_data},
            )
        )
    except Exception as exc:
        return ActionResult("edit_where", None, "failed", f"API error: {exc}")

    snapshots = []
    if include_snapshots:
        for row_num in targets:
            after = _read_row_snapshot(service, spreadsheet_id, worksheet, row_num, header_count)
            snapshots.append({"row": row_num, "snapshot_before": snapshots_before[row_num], "snapshot_after": after})

    meta: Dict[str, Any] = {"edited": len(targets), "matches": targets, "ranges": edited_ranges}
    if include_snapshots:
        meta["snapshots"] = snapshots
    return ActionResult("edit_where", None, "success", None, meta)

def action_delete_where(
    service: Any,
    spreadsheet_id: str,
    worksheet: str,
    where: Dict[str, Any],
    header_count: int,
    limit: Optional[int] = None,
    offset: int = 0,
    include_snapshots: bool = False,
    cached_vals: Optional[List[List[str]]] = None,
) -> ActionResult:
    vals = cached_vals if cached_vals is not None else _get_tab_values(service, spreadsheet_id, worksheet)
    if not vals:
        return ActionResult("delete_where", None, "failed", "Tab leer oder nicht lesbar")

    headers = vals[0]
    header_idx = {h: i for i, h in enumerate(headers)}
    pred = _compile_where(where)

    rows = []
    for i, row in enumerate(vals[1:], start=2):
        if pred(header_idx, row):
            rows.append(i)

    if offset:
        rows = rows[offset:]
    if isinstance(limit, int) and limit >= 0:
        rows = rows[:limit]

    if not rows:
        return ActionResult("delete_where", None, "success", None, {"deleted": 0, "matches": []})

    # Wichtig: von unten nach oben löschen, damit Indizes stabil bleiben
    rows_sorted = sorted(rows, reverse=True)
    deleted = 0
    snapshots = []
    # sheetId ermitteln
    meta_all = service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties").execute()
    sheet_id = None
    for sh in meta_all.get("sheets", []):
        props = sh.get("properties", {})
        if props.get("title") == worksheet:
            sheet_id = props.get("sheetId")
            break
    if sheet_id is None:
        return ActionResult("delete_where", None, "failed", f"Worksheet '{worksheet}' nicht gefunden")

    for row_num in rows_sorted:
        before = _read_row_snapshot(service, spreadsheet_id, worksheet, row_num, header_count) if include_snapshots else []
        body = {
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row_num - 1,
                            "endIndex": row_num,
                        }
                    }
                }
            ]
        }
        service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()
        deleted += 1
        if include_snapshots:
            snapshots.append({"row": row_num, "snapshot_before": before})

    meta = {"deleted": deleted, "matches": rows}
    if include_snapshots:
        meta["snapshots"] = snapshots
    return ActionResult("delete_where", None, "success", None, meta)

def _row_a1_range(worksheet: str, row_num: int, header_count: int) -> str:
    # Spalten A..Z..AA etc. bis header_count
    def col_letter(idx: int) -> str:
        # idx: 0-basiert
        n = idx
        s = ""
        while True:
            n, r = divmod(n, 26)
            s = chr(ord('A') + r) + s
            if n == 0:
                break
            n -= 1
        return s
    end_col = col_letter(max(header_count - 1, 0))
    return f"{worksheet}!A{row_num}:{end_col}{row_num}"

def _read_row_snapshot(service: Any, spreadsheet_id: str, worksheet: str, row_num: int, header_count: int) -> List[str]:
    if row_num is None or row_num < 1 or header_count <= 0:
        return []
    a1 = _row_a1_range(worksheet, row_num, header_count)
    try:
        resp = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=a1
        ).execute()
        vals = resp.get("values", [])
        return vals[0] if vals else []
    except Exception:
        return []

# --------- Row Actions ---------

def perform_append(
    service: Any,
    spreadsheet_id: str,
    worksheet_name: str,
    col_map: Dict[str, str],
    header_count: int,
    values: Dict[str, Any],
    include_snapshots: bool = True,
) -> ActionResult:
    # Validierung Spalten
    for column in values.keys():
        if column not in col_map:
            return ActionResult(
                action="append",
                row=None,
                status="failed",
                error=f"Spalte '{column}' fehlt",
            )

    # Werte in Reihenfolge der Header bauen
    ordered = ["" for _ in range(header_count)]
    for i, name in enumerate(col_map.keys()):
        ordered[i] = values.get(name, "")

    try:
        result = (
            service.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range=f"{worksheet_name}!1:1",  # sorgt für richtige Tabelle
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                includeValuesInResponse=True,
                body={"values": [ordered]},
            ).execute()
        )
        updates = result.get("updates", {})
        updated_range = updates.get("updatedRange")
        updated_row = parse_updated_row_from_range(updated_range) if updated_range else None

        meta: Dict[str, Any] = {"updated_range": updated_range}
        if include_snapshots and updated_row:
            after = _read_row_snapshot(service, spreadsheet_id, worksheet_name, updated_row, header_count)
            meta["snapshot_after"] = after

        return ActionResult("append", updated_row, "success", None, meta)
    except Exception as exc:
        return ActionResult("append", None, "failed", f"API error: {exc}")

def perform_edit_or_comment(
    service: Any,
    spreadsheet_id: str,
    worksheet_name: str,
    col_map: Dict[str, str],
    action_type: str,
    row_num: int,
    values: Dict[str, Any],
    include_snapshots: bool = True,
) -> ActionResult:
    if not isinstance(row_num, int) or row_num < 2:
        return ActionResult(action_type, row_num if isinstance(row_num, int) else None, "failed", "Ungültige Zeilennummer")

    for column in values.keys():
        if column not in col_map:
            return ActionResult(action_type, row_num, "failed", f"Spalte '{column}' fehlt")

    # snapshot_before
    snapshot_before: List[str] = []
    header_count = len(col_map.keys())
    if include_snapshots:
        snapshot_before = _read_row_snapshot(service, spreadsheet_id, worksheet_name, row_num, header_count)

    # Daten für batchUpdateValues
    data = []
    for col_name, val in values.items():
        col_letter = col_map[col_name]
        a1_range = f"{worksheet_name}!{col_letter}{row_num}:{col_letter}{row_num}"
        data.append({"range": a1_range, "values": [[val]]})

    try:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()

        meta: Dict[str, Any] = {"ranges": [d["range"] for d in data]}
        if include_snapshots:
            after = _read_row_snapshot(service, spreadsheet_id, worksheet_name, row_num, header_count)
            meta["snapshot_before"] = snapshot_before
            meta["snapshot_after"] = after

        return ActionResult(action_type, row_num, "success", None, meta)
    except Exception as exc:
        return ActionResult(action_type, row_num, "failed", f"API error: {exc}")

def perform_delete(
    service: Any,
    spreadsheet_id: str,
    sheet_id: int,
    worksheet_name: str,
    row_num: int,
    header_count: int,
    include_snapshots: bool = True,
) -> ActionResult:
    if not isinstance(row_num, int) or row_num < 2:
        return ActionResult("delete", row_num if isinstance(row_num, int) else None, "failed", "Ungültige Zeilennummer")

    snapshot_before: List[str] = []
    if include_snapshots:
        snapshot_before = _read_row_snapshot(service, spreadsheet_id, worksheet_name, row_num, header_count)

    try:
        body = {
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row_num - 1,
                            "endIndex": row_num,
                        }
                    }
                }
            ]
        }
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body=body
        ).execute()

        meta: Dict[str, Any] = {}
        if include_snapshots:
            meta["snapshot_before"] = snapshot_before
            # snapshot_after: Zeile existiert i. d. R. nicht mehr → weglassen

        return ActionResult("delete", row_num, "success", None, meta)
    except Exception as exc:
        return ActionResult("delete", row_num, "failed", f"API error: {exc}")

# --------- Tab Actions ---------

def action_create_tab(service: Any, spreadsheet_id: str, worksheet: str, schema: Optional[List[str]] = None, index: Optional[int] = None) -> ActionResult:
    try:
        req = {
            "requests": [
                {"addSheet": {"properties": {"title": worksheet}}}
            ]
        }
        if isinstance(index, int):
            req["requests"][0]["addSheet"]["properties"]["index"] = index
        resp = service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=req).execute()
        # Header optional setzen
        if schema:
            rng = f"{worksheet}!1:1"
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=rng,
                valueInputOption="RAW",
                body={"values": [schema]}
            ).execute()
        return ActionResult("create_tab", None, "success", None, {"worksheet": worksheet, "index": index, "schema": schema})
    except Exception as exc:
        return ActionResult("create_tab", None, "failed", f"API error: {exc}")

def action_delete_tab(service: Any, spreadsheet_id: str, worksheet: str) -> ActionResult:
    # sheetId ermitteln
    try:
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties").execute()
        sid = None
        for sh in meta.get("sheets", []):
            props = sh.get("properties", {})
            if props.get("title") == worksheet:
                sid = props.get("sheetId")
                break
        if sid is None:
            return ActionResult("delete_tab", None, "failed", f"Worksheet '{worksheet}' nicht gefunden")
        req = {"requests": [{"deleteSheet": {"sheetId": sid}}]}
        service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=req).execute()
        return ActionResult("delete_tab", None, "success", None, {"worksheet": worksheet})
    except Exception as exc:
        return ActionResult("delete_tab", None, "failed", f"API error: {exc}")

def action_rename_tab(service: Any, spreadsheet_id: str, worksheet: str, new_name: str) -> ActionResult:
    try:
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties").execute()
        sid = None
        for sh in meta.get("sheets", []):
            props = sh.get("properties", {})
            if props.get("title") == worksheet:
                sid = props.get("sheetId")
                break
        if sid is None:
            return ActionResult("rename_tab", None, "failed", f"Worksheet '{worksheet}' nicht gefunden")
        req = {"requests": [{"updateSheetProperties": {"properties": {"sheetId": sid, "title": new_name}, "fields": "title"}}]}
        service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=req).execute()
        return ActionResult("rename_tab", None, "success", None, {"from": worksheet, "to": new_name})
    except Exception as exc:
        return ActionResult("rename_tab", None, "failed", f"API error: {exc}")

def action_set_headers(service: Any, spreadsheet_id: str, worksheet: str, schema: List[str]) -> ActionResult:
    if not isinstance(schema, list) or not schema:
        return ActionResult("set_headers", None, "failed", "schema[] fehlt oder ist leer")
    try:
        rng = f"{worksheet}!1:1"
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=rng,
            valueInputOption="RAW",
            body={"values": [schema]}
        ).execute()
        return ActionResult("set_headers", 1, "success", None, {"worksheet": worksheet, "schema": schema})
    except Exception as exc:
        return ActionResult("set_headers", None, "failed", f"API error: {exc}")
