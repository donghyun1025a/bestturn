"""대시보드 실행기.

PYTHONPATH 를 따로 잡지 않아도 되도록 src 를 직접 경로에 넣습니다.
윈도우·맥·리눅스 모두 `python run.py` 로 실행됩니다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from eta_ingest.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
