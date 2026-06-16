# gsi/validators.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    action: str
    row: Optional[int]
    status: str                 # "success" | "failed"
    error: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"action": self.action, "status": self.status}
        if self.row is not None:
            out["row"] = self.row
        if self.status == "failed" and self.error:
            out["error"] = self.error
        if self.details is not None:
            out["details"] = self.details
        return out


# ---- Erlaubte Aktionen ------------------------------------------------------
ROW_ACTIONS = {
    "append", "append_rows",
    "edit", "delete", "comment",
    "find_rows", "edit_where", "delete_where",
    "find_first_empty", "get_tab", "get_headers",
}
TAB_ACTIONS = {"create_tab", "delete_tab", "rename_tab", "set_headers", "list_tabs"}
ACCEPTED_TYPES = ROW_ACTIONS | TAB_ACTIONS


# ---- Helpers ----------------------------------------------------------------

def _is_int(v: Any) -> bool:
    try:
        if isinstance(v, bool):
            return False
        if isinstance(v, int):
            return True
        if isinstance(v, str):
            return v.isdigit()
        return False
    except Exception:
        return False


def _unknown_columns(keys: List[str], headers: Optional[List[str]]) -> List[str]:
    if not headers:
        return []
    header_set = set(headers)
    return sorted([k for k in set(keys) if k not in header_set])


# ---- Haupt-Validierung ------------------------------------------------------

def prevalidate_actions(
    actions: List[dict],
    headers: Optional[List[str]],
    default_ws: Optional[str],
    diagnostics: List[Dict[str, Any]],
) -> Tuple[List[ActionResult], List[dict]]:
    """
    Validiert die vom Nutzer gelieferten Actions/Operations und gibt
    (1) unmittelbare Fehlermeldungen (ActionResult) sowie
    (2) die verbleibenden, normalisierten Actions zurück.
    """
    pre_results: List[ActionResult] = []
    remaining: List[dict] = []

    # Debug-Level hier nicht überschreiben; nur loggen wenn konfiguriert.
    for i, a in enumerate(actions):
        t = a.get("type") or a.get("op")  # failsafe: 'op' alias akzeptieren
        if t and "type" not in a:
            # nachziehen, damit downstream konsistent ist
            a = dict(a)
            a["type"] = t

        if not t:
            diagnostics.append({
                "level": "error",
                "code": "missing_type",
                "message": f"Action {i}: 'type' fehlt."
            })
            pre_results.append(ActionResult("unknown", None, "failed", "Aktionstyp fehlt"))
            continue

        if t not in ACCEPTED_TYPES:
            diagnostics.append({
                "level": "error",
                "code": "unknown_type",
                "message": f"Action {i}: unbekannter Typ '{t}'."
            })
            pre_results.append(ActionResult(t, None, "failed", f"Unbekannter Aktionstyp '{t}'"))
            continue

        ws = a.get("worksheet", default_ws)

        # -------- Zeilenaktionen (mit expliziter row) ------------------------
        if t in {"edit", "comment", "delete"}:
            row = a.get("row")
            if not isinstance(row, int) or row < 1:
                diagnostics.append({
                    "level": "error",
                    "code": "row_required",
                    "message": f"Action {i}: 'row' muss Ganzzahl sein (≥1)."
                })
                pre_results.append(ActionResult(t, None, "failed", "Zeilennummer fehlt oder ungültig"))
                continue

            if t in {"edit", "comment"}:
                vals = a.get("values")
                if not isinstance(vals, dict):
                    diagnostics.append({
                        "level": "error",
                        "code": "values_required",
                        "message": f"Action {i}: 'values' muss Objekt sein."
                    })
                    pre_results.append(ActionResult(t, row, "failed", "'values' fehlt/ungültig"))
                    continue
                unk = _unknown_columns(list(vals.keys()), headers)
                if unk:
                    diagnostics.append({
                        "level": "error",
                        "code": "unknown_columns",
                        "message": f"Action {i}: unbekannte Spalten {unk}."
                    })
                    pre_results.append(ActionResult(t, row, "failed", f"Unbekannte Spalten: {', '.join(unk)}"))
                    continue

        # -------- Append (einzelne Zeile) -----------------------------------
        if t == "append":
            vals = a.get("values")
            if not isinstance(vals, dict):
                diagnostics.append({
                    "level": "error",
                    "code": "values_required",
                    "message": f"Action {i}: 'values' muss Objekt sein."
                })
                pre_results.append(ActionResult(t, None, "failed", "'values' fehlt/ungültig"))
                continue
            unk = _unknown_columns(list(vals.keys()), headers)
            if unk:
                diagnostics.append({
                    "level": "error",
                    "code": "unknown_columns",
                    "message": f"Action {i}: unbekannte Spalten {unk}."
                })
                pre_results.append(ActionResult(t, None, "failed", f"Unbekannte Spalten: {', '.join(unk)}"))
                continue

        # -------- Append (mehrere Zeilen) -----------------------------------
        if t == "append_rows":
            rows = a.get("rows")
            if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
                diagnostics.append({
                    "level": "error",
                    "code": "rows_required",
                    "message": f"Action {i}: 'rows' muss eine nicht-leere Liste von Objekte sein."
                })
                pre_results.append(ActionResult(t, None, "failed", "Feld 'rows' fehlt/ungültig/leer"))
                continue

            # Optional: header_row akzeptieren (nur Typ-Check)
            if "header_row" in a and not _is_int(a["header_row"]):
                diagnostics.append({
                    "level": "error",
                    "code": "header_row_invalid",
                    "message": f"Action {i}: 'header_row' muss Ganzzahl sein."
                })
                pre_results.append(ActionResult(t, None, "failed", "Ungültiges 'header_row'"))
                continue

            # Spaltenprüfung über alle Zeilen
            all_keys = set()
            for r in rows:
                all_keys.update(r.keys())
            unk = _unknown_columns(list(all_keys), headers)
            if unk:
                diagnostics.append({
                    "level": "error",
                    "code": "unknown_columns",
                    "message": f"Action {i}: unbekannte Spalten {unk}."
                })
                pre_results.append(ActionResult(t, None, "failed", f"Unbekannte Spalten: {', '.join(unk)}"))
                continue



        if t == "get_headers":
            # nichts weiter – worksheet wird außerhalb geprüft
            pass

        if t == "get_tab":
            off = a.get("offset")
            if off is not None and not _is_int(off):
                diagnostics.append({"level":"error","code":"offset_invalid","message":f"Action {i}: 'offset' muss int sein."})
                pre_results.append(ActionResult(t, None, "failed", "Ungültiger 'offset'"))
                continue
            lim = a.get("limit")
            if lim is not None and not _is_int(lim):
                diagnostics.append({"level":"error","code":"limit_invalid","message":f"Action {i}: 'limit' muss int sein."})
                pre_results.append(ActionResult(t, None, "failed", "Ungültiges 'limit'"))
                continue
            ao = a.get("as_objects")
            if ao is not None and not isinstance(ao, bool):
                diagnostics.append({"level":"error","code":"as_objects_invalid","message":f"Action {i}: 'as_objects' muss bool sein."})
                pre_results.append(ActionResult(t, None, "failed", "Ungültiges 'as_objects'"))
                continue



        # -------- Filter-/Bulk-Aktionen -------------------------------------
        if t == "find_rows":
            where = a.get("where")
            if where is not None and not isinstance(where, dict):
                diagnostics.append({
                    "level": "error",
                    "code": "where_invalid",
                    "message": f"Action {i}: 'where' muss Objekt sein (oder fehlen)."
                })
                pre_results.append(ActionResult(t, None, "failed", "Ungültiges 'where'"))
            off = a.get("offset")
            if off is not None and not _is_int(off):
                diagnostics.append({
                    "level": "error",
                    "code": "offset_invalid",
                    "message": f"Action {i}: 'offset' muss int sein."
                })
                pre_results.append(ActionResult(t, None, "failed", "Ungültiger 'offset'"))
            lim = a.get("limit")
            if lim is not None and not _is_int(lim):
                diagnostics.append({
                    "level": "error",
                    "code": "limit_invalid",
                    "message": f"Action {i}: 'limit' muss int sein."
                })
                pre_results.append(ActionResult(t, None, "failed", "Ungültiges 'limit'"))

            # Wenn bereits Fehler in diesem Block erzeugt wurden, nicht weiterreichen
            if pre_results and pre_results[-1].action == t and pre_results[-1].status == "failed":
                continue

        if t == "find_first_empty":
            header = a.get("header")
            if not isinstance(header, str) or not header.strip():
                diagnostics.append({"level":"error","code":"header_required","message":f"Action {i}: 'header' (String) erforderlich."})
                pre_results.append(ActionResult(t, None, "failed", "'header' fehlt"))
                continue
            sr = a.get("start_row")
            if sr is not None and not _is_int(sr):
                diagnostics.append({"level":"error","code":"start_row_invalid","message":f"Action {i}: 'start_row' muss int sein."})
                pre_results.append(ActionResult(t, None, "failed", "Ungültiges 'start_row'"))
                continue

        if t == "edit_where":
            where = a.get("where")
            vals = a.get("values")
            if not isinstance(where, dict):
                diagnostics.append({
                    "level": "error",
                    "code": "where_required",
                    "message": f"Action {i}: 'where' (Objekt) erforderlich."
                })
                pre_results.append(ActionResult(t, None, "failed", "'where' fehlt"))
                continue
            if not isinstance(vals, dict):
                diagnostics.append({
                    "level": "error",
                    "code": "values_required",
                    "message": f"Action {i}: 'values' (Objekt) erforderlich."
                })
                pre_results.append(ActionResult(t, None, "failed", "'values' fehlt"))
                continue
            unk = _unknown_columns(list(vals.keys()), headers)
            if unk:
                diagnostics.append({
                    "level": "error",
                    "code": "unknown_columns",
                    "message": f"Action {i}: unbekannte Spalten {unk}."
                })
                pre_results.append(ActionResult(t, None, "failed", f"Unbekannte Spalten: {', '.join(unk)}"))
                continue
            # optionale Grenzen
            lim = a.get("limit")
            if lim is not None and not _is_int(lim):
                diagnostics.append({
                    "level": "error",
                    "code": "limit_invalid",
                    "message": f"Action {i}: 'limit' muss int sein."
                })
                pre_results.append(ActionResult(t, None, "failed", "Ungültiges 'limit'"))
                continue

        if t == "delete_where":
            where = a.get("where")
            if not isinstance(where, dict):
                diagnostics.append({
                    "level": "error",
                    "code": "where_required",
                    "message": f"Action {i}: 'where' (Objekt) erforderlich."
                })
                pre_results.append(ActionResult(t, None, "failed", "'where' fehlt"))
                continue
            # optionale Grenzen
            lim = a.get("limit")
            if lim is not None and not _is_int(lim):
                diagnostics.append({
                    "level": "error",
                    "code": "limit_invalid",
                    "message": f"Action {i}: 'limit' muss int sein."
                })
                pre_results.append(ActionResult(t, None, "failed", "Ungültiges 'limit'"))
                continue

        # -------- Tab-Management --------------------------------------------
        if t in TAB_ACTIONS:
            if t != "list_tabs" and not ws:
                diagnostics.append({
                    "level": "error",
                    "code": "worksheet_required",
                    "message": f"Action {i}: 'worksheet' fehlt."
                })
                pre_results.append(ActionResult(t, None, "failed", "Worksheet-Name fehlt"))
                continue
            if t == "rename_tab" and not a.get("new_name"):
                diagnostics.append({
                    "level": "error",
                    "code": "new_name_required",
                    "message": f"Action {i}: 'new_name' fehlt."
                })
                pre_results.append(ActionResult(t, None, "failed", "Neuer Name fehlt"))
                continue
            if t == "set_headers":
                schema = a.get("schema")
                if not isinstance(schema, list) or not schema:
                    diagnostics.append({
                        "level": "error",
                        "code": "schema_required",
                        "message": f"Action {i}: 'schema' (Array) erforderlich."
                    })
                    pre_results.append(ActionResult(t, None, "failed", "Schema fehlt oder leer"))
                    continue

        # -------- Normalisierte Action weiterreichen -------------------------
        a_norm = dict(a)
        if ws is not None:
            a_norm["worksheet"] = ws

        # ints normalisieren
        for k in ("offset", "limit", "header_row"):
            if k in a_norm and _is_int(a_norm[k]):
                a_norm[k] = int(a_norm[k])

        logger.debug("[validators] accept type=%r ws=%r", t, ws)
        remaining.append(a_norm)

    return pre_results, remaining
