"""UI 에서 입력한 설정을 환경변수 위에 덮어씁니다.

환경변수는 기본값, DB(app_config)에 저장된 값이 우선입니다.
자격증명은 저장만 하고 API 응답으로는 절대 내보내지 않습니다.
"""
from __future__ import annotations

from dataclasses import replace

from .settings import DEFAULT_AIRLINES, Settings
from .storage import EtaStore

SECRET_KEYS = frozenset({"firehose_password", "data_go_kr_key"})
EDITABLE_KEYS = ("firehose_username", "firehose_password", "data_go_kr_key", "airlines", "airport")


def _airlines(raw: str) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for chunk in raw.replace(";", ",").split(","):
        code = chunk.strip().upper()
        if code:
            seen[code] = None
    return tuple(seen)


def effective(base: Settings, store: EtaStore) -> Settings:
    stored = store.config()
    return replace(
        base,
        username=stored.get("firehose_username") or base.username,
        password=stored.get("firehose_password") or base.password,
        service_key=stored.get("data_go_kr_key") or base.service_key,
        airlines=_airlines(stored["airlines"]) if stored.get("airlines") else base.airlines,
        airport=(stored.get("airport") or base.airport).strip().upper(),
    )


def save(store: EtaStore, payload: dict) -> None:
    """빈 문자열은 "변경 없음"으로 보고 건너뜁니다 (비밀번호를 지우지 않기 위해)."""
    values = {}
    for key in EDITABLE_KEYS:
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            values[key] = raw.strip()
    if values:
        store.save_config(values)


def public_view(settings: Settings) -> dict:
    """UI 로 돌려줄 설정. 비밀 값은 입력 여부만 알립니다."""
    return {
        "firehose_username": settings.username,
        "has_firehose_password": bool(settings.password),
        "has_data_go_kr_key": bool(settings.service_key),
        "airlines": list(settings.airlines),
        "airport": settings.airport,
        "default_airlines": DEFAULT_AIRLINES.split(","),
    }
