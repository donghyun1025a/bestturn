"""운항 현황(remark) 해석과 book 감시용 변동 비교."""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..api.models import Flight

# remark 는 기관에서 자유 문자열로 내려오므로 부분일치로 분류합니다.
_STATUS_EMOJI = [
    ("결항", "❌"), ("회항", "↩️"), ("지연", "⏰"), ("취소", "❌"),
    ("탑승마감", "🚪"), ("마감", "🚪"), ("탑승중", "🛫"), ("보딩", "🛫"),
    ("탑승준비", "🕐"), ("출발", "🛫"), ("이륙", "🛫"),
    ("착륙", "🛬"), ("도착", "🛬"), ("접현", "🛬"),
    ("수속", "🧳"), ("체크인", "🧳"),
]

# 이 상태가 되면 감시를 자동 종료해도 되는 종료 상태
TERMINAL_REMARKS = ("결항", "취소", "출발", "이륙", "회항", "도착", "수하물수취")


def status_emoji(remark: str | None) -> str:
    if not remark:
        return "•"
    for keyword, emoji in _STATUS_EMOJI:
        if keyword in remark:
            return emoji
    return "•"


def is_terminal_status(flight: Flight) -> bool:
    remark = flight.remark or ""
    return any(keyword in remark for keyword in TERMINAL_REMARKS)


def should_stop_watching(flight: Flight, *, now: datetime | None = None, grace_minutes: int = 90) -> bool:
    """종료 상태 + 예정시각 경과 후 유예시간이 지나면 자동 종료."""
    now = now or datetime.now()
    if flight.remark and any(k in flight.remark for k in ("결항", "취소")):
        return True
    reference = flight.best_dt
    if reference is None:
        return False
    if now < reference + timedelta(minutes=grace_minutes):
        return False
    return is_terminal_status(flight) or now > reference + timedelta(hours=6)


@dataclass(frozen=True)
class FieldChange:
    field: str
    before: str | None
    after: str | None

    def render(self) -> str:
        """텔레그램 HTML 파스모드용 한 줄."""
        before = html.escape(self.before or "미정")
        after = html.escape(self.after or "미정")
        return f"• {html.escape(self.field)}: {before} → <b>{after}</b>"


def diff_flights(previous: dict[str, str | None], current: dict[str, str | None]) -> list[FieldChange]:
    """이전 스냅샷과 현재 값을 비교. 값이 새로 채워진 경우도 변동으로 봅니다."""
    changes: list[FieldChange] = []
    for field, after in current.items():
        before = previous.get(field)
        if (before or None) == (after or None):
            continue
        changes.append(FieldChange(field, before, after))
    return changes


def adaptive_interval_minutes(flight: Flight | None, *, now: datetime | None = None) -> int:
    """출발/도착 시각이 가까울수록 촘촘하게 감시(일일 호출 한도 보호)."""
    now = now or datetime.now()
    reference = flight.best_dt if flight else None
    if reference is None:
        return 15
    minutes_left = (reference - now).total_seconds() / 60
    if minutes_left <= -30:
        return 10
    if minutes_left <= 60:
        return 2
    if minutes_left <= 180:
        return 5
    if minutes_left <= 480:
        return 10
    return 20
