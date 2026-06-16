from __future__ import annotations
import logging, sys, os

def setup_logging(debug: bool = False, log_file: str | None = None) -> None:
    logger = logging.getLogger("gpt_sheet")
    logger.setLevel(logging.DEBUG if (debug or os.environ.get("GPT_SHEETS_DEBUG") == "1") else logging.INFO)
    logger.handlers.clear()

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(sh)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(fh)

    if debug or os.environ.get("GPT_SHEETS_DEBUG") == "1":
        logger.debug("Debug logging enabled")
