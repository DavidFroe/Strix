#!/usr/bin/env python3
from __future__ import annotations
import logging
from gsi.cli import parse_args
from gsi.logging_utils import setup_logging
from gsi.engine import run

LOG = logging.getLogger("gpt_sheet")

def main() -> None:
    args = parse_args()
    setup_logging(debug=args.debug, log_file=args.log_file)
    run(args)

if __name__ == "__main__":
    main()
