"""python run.py [ui|doctor|ingest]

ui     — 웹 대시보드. 자격증명 입력·수집 시작/중지·조회·대조를 모두 여기서 합니다 (기본값).
doctor — 접속이 안 될 때 DNS·포트·TLS 어디서 막히는지 진단합니다.
ingest — UI 없이 수집만. 자격증명은 환경변수로 받습니다 (서버 상주용).
"""
from __future__ import annotations

import logging
import sys
import webbrowser

from . import api, diagnostics, ingest
from .settings import Settings


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "ui"
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if command in ("ui", "api"):
        url = f"http://{settings.api_host}:{settings.api_port}"
        logging.info("대시보드 — %s", url)
        webbrowser.open(url)
        api.serve(settings)
    elif command == "doctor":
        return diagnostics.run(settings)
    elif command == "ingest":
        ingest.run(settings)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
