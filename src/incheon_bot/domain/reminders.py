"""출발/도착 예정시각 기준 사전 알림 규칙."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from ..api.models import Flight


@dataclass(frozen=True)
class ReminderRule:
    key: str
    minutes_before: int     # 양수 = N분 전, 음수 = N분 후
    title: str
    body: str
    include_route: bool = False

    def fire_at(self, reference: datetime) -> datetime:
        return reference - timedelta(minutes=self.minutes_before)

    @property
    def offset_label(self) -> str:
        if self.minutes_before >= 0:
            return f"T-{self.minutes_before}분"
        return f"T+{abs(self.minutes_before)}분"


@dataclass(frozen=True)
class DueReminder:
    key: str
    title: str
    body: str
    include_route: bool
    fire_at: datetime


class ReminderConfig:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.reload()

    def reload(self) -> None:
        data = {}
        if self.path.exists():
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self.catch_up_minutes = int(data.get("catch_up_minutes", 20))
        self._rules = {
            "OB": self._parse(data.get("outbound")),
            "IB": self._parse(data.get("inbound")),
        }

    @staticmethod
    def _parse(raw) -> tuple[ReminderRule, ...]:
        rules = []
        for item in raw or []:
            if not item.get("key"):
                continue
            rules.append(
                ReminderRule(
                    key=str(item["key"]),
                    minutes_before=int(item.get("minutes_before", 0)),
                    title=str(item.get("title", item["key"])),
                    body=str(item.get("body", "")),
                    include_route=bool(item.get("include_route", False)),
                )
            )
        # 먼저 발송할 것(=시각이 이른 것)부터
        return tuple(sorted(rules, key=lambda r: -r.minutes_before))

    def rules_for(self, direction: str) -> tuple[ReminderRule, ...]:
        return self._rules.get(direction, ())

    def initial_sent_keys(self, flight: Flight, *, now: datetime | None = None) -> list[str]:
        """등록 시점에 이미 유예시간을 넘겨 지나간 알림은 발송 완료로 간주합니다."""
        now = now or datetime.now()
        reference = flight.best_dt
        if reference is None:
            return []
        cutoff = timedelta(minutes=self.catch_up_minutes)
        return [
            rule.key
            for rule in self.rules_for(flight.direction)
            if now > rule.fire_at(reference) + cutoff
        ]

    def evaluate(
        self, flight: Flight, sent_keys: list[str], *, now: datetime | None = None
    ) -> tuple[list[DueReminder], list[str]]:
        """(지금 보낼 알림, 시기를 놓쳐 건너뛸 알림 키) 를 반환합니다."""
        now = now or datetime.now()
        reference = flight.best_dt
        if reference is None:
            return [], []
        already = set(sent_keys)
        cutoff = timedelta(minutes=self.catch_up_minutes)
        due: list[DueReminder] = []
        expired: list[str] = []
        for rule in self.rules_for(flight.direction):
            if rule.key in already:
                continue
            fire_at = rule.fire_at(reference)
            if now < fire_at:
                continue
            if now > fire_at + cutoff:
                expired.append(rule.key)
                continue
            due.append(DueReminder(rule.key, rule.title, rule.body, rule.include_route, fire_at))
        return due, expired


def custom_reminder_due(
    minutes_before: int, flight: Flight, *, now: datetime | None = None, catch_up_minutes: int = 20
) -> bool:
    now = now or datetime.now()
    reference = flight.best_dt
    if reference is None:
        return False
    fire_at = reference - timedelta(minutes=minutes_before)
    return fire_at <= now <= fire_at + timedelta(minutes=catch_up_minutes)
