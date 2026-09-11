"""환경변수 기반 실행 설정."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_AIRLINES = "WE,8M,AS,AA,WS"
DEFAULT_AIRPORT = "ICN"


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _codes(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip() or default
    seen: dict[str, None] = {}
    for chunk in raw.replace(";", ",").split(","):
        code = chunk.strip().upper()
        if code:
            seen[code] = None
    return tuple(seen)


@dataclass(frozen=True)
class Settings:
    username: str = field(default_factory=lambda: os.getenv("FIREHOSE_USERNAME", "").strip())
    password: str = field(default_factory=lambda: os.getenv("FIREHOSE_PASSWORD", "").strip())
    host: str = field(default_factory=lambda: os.getenv("FIREHOSE_HOST", "firehose.flightaware.com").strip())
    port: int = field(default_factory=lambda: _int("FIREHOSE_PORT", 1501))
    airlines: tuple[str, ...] = field(default_factory=lambda: _codes("ETA_AIRLINES", DEFAULT_AIRLINES))
    airport: str = field(default_factory=lambda: os.getenv("ETA_AIRPORT", DEFAULT_AIRPORT).strip().upper())
    db_path: Path = field(default_factory=lambda: Path(os.getenv("ETA_DB_PATH", "data/eta.db")))
    api_host: str = field(default_factory=lambda: os.getenv("ETA_API_HOST", "127.0.0.1").strip())
    api_port: int = field(default_factory=lambda: _int("ETA_API_PORT", 8800))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())

    def validate(self) -> None:
        missing = [
            name
            for name, value in (("FIREHOSE_USERNAME", self.username), ("FIREHOSE_PASSWORD", self.password))
            if not value
        ]
        if missing:
            raise SystemExit(f"환경변수가 설정되지 않았습니다: {', '.join(missing)} (.env 참고)")
