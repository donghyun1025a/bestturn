"""python -m eta_ingest ingest | api"""
from __future__ import annotations

import logging
import sys

from . import api, ingest
from .settings import Settings


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "ingest"
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if command == "ingest":
        ingest.run(settings)
    elif command == "api":
        logging.info("조회 API — http://%s:%d", settings.api_host, settings.api_port)
        api.serve(settings)
    else:
        print("사용법: python -m eta_ingest [ingest|api]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
