"""환경변수 기반 실행 설정."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    out = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            try:
                out.add(int(chunk))
            except ValueError:
                continue
    return frozenset(out)


@dataclass(frozen=True)
class Settings:
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
    service_key: str = field(default_factory=lambda: os.getenv("DATA_GO_KR_SERVICE_KEY", "").strip())
    allowed_user_ids: frozenset[int] = field(default_factory=lambda: _ids("ALLOWED_USER_IDS"))
    db_path: Path = field(default_factory=lambda: Path(os.getenv("IIA_DB_PATH", "data/bookings.db")))
    config_dir: Path = field(default_factory=lambda: Path(os.getenv("IIA_CONFIG_DIR", "config")))
    flight_daily_budget: int = field(default_factory=lambda: _int("IIA_FLIGHT_DAILY_BUDGET", 450))
    congestion_daily_budget: int = field(default_factory=lambda: _int("IIA_CONGESTION_DAILY_BUDGET", 900))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("TELEGRAM_BOT_TOKEN", self.telegram_token),
                ("DATA_GO_KR_SERVICE_KEY", self.service_key),
            )
            if not value
        ]
        if missing:
            raise SystemExit(f"환경변수가 설정되지 않았습니다: {', '.join(missing)} (.env 참고)")

    def is_allowed(self, user_id: int | None) -> bool:
        if not self.allowed_user_ids:
            return True
        return user_id is not None and user_id in self.allowed_user_ids
