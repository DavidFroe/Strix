"""Datenbankzugriff für den Lumpensammler-Agenten – Wrapper um db.datenbank."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from db.datenbank import (  # noqa: F401
    _init_conf, _update_row,
    get_next_ref, reserve_ref, check_exists, check_url_exists, get_stellen_by_firma,
    get_row, get_all_rows, get_unprocessed, fill_slot,
    add_entry, mark_lumpensammler_done, clear_review,
    STATUS_NEU,
)

_init_conf(Path(__file__).parent.parent)
