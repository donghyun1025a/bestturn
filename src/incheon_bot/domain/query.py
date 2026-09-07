"""자유입력 파싱: 'WE501', 'WE 501 0908', '20260908 KE86', 'WE501 내일'."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

FLIGHT_RE = re.compile(r"^(?P<carrier>[A-Z][A-Z0-9]|[A-Z0-9][A-Z])\s*-?\s*(?P<num>\d{1,4})$")

RELATIVE_DAYS = {
    "오늘": 0, "today": 0, "금일": 0,
    "내일": 1, "tomorrow": 1, "명일": 1,
    "모레": 2, "내일모레": 2,
    "어제": -1, "yesterday": -1, "전일": -1,
    "그제": -2, "그저께": -2,
}

# 운항현황 API 제공 범위: 조회일 기준 -3 ~ +6일
MIN_OFFSET, MAX_OFFSET = -3, 6


class QueryError(ValueError):
    """사용자 입력 오류."""


@dataclass(frozen=True)
class FlightQuery:
    flight_no: str          # 정규화된 편명 (예: WE501)
    day: date

    @property
    def search_date(self) -> str:
        return self.day.strftime("%Y%m%d")


def _parse_date_token(token: str, today: date) -> date | None:
    key = token.lower()
    if key in RELATIVE_DAYS:
        return today + timedelta(days=RELATIVE_DAYS[key])
    digits = re.sub(r"[^0-9]", "", token)
    if len(digits) == 8:                      # 20260908
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            return None
    if len(digits) == 4:
        # 0908 (MMDD) — 연도는 오늘 기준으로 가장 가까운 해를 선택
        month, day = int(digits[:2]), int(digits[2:])
        for year in (today.year, today.year + 1, today.year - 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if abs((candidate - today).days) <= 180:
                return candidate
    return None


def parse_query(text: str, *, today: date | None = None) -> FlightQuery:
    today = today or date.today()
    tokens = [t for t in re.split(r"[\s,/]+", (text or "").strip()) if t]
    if not tokens:
        raise QueryError("편명을 입력해 주세요. 예) `WE501` 또는 `WE501 0908`")

    flight_no: str | None = None
    day: date | None = None
    leftovers: list[str] = []

    # 'WE 501' 처럼 편명이 두 토큰으로 나뉜 경우를 먼저 합칩니다.
    merged: list[str] = []
    i = 0
    while i < len(tokens):
        cur = tokens[i].upper()
        nxt = tokens[i + 1].upper() if i + 1 < len(tokens) else ""
        if re.fullmatch(r"[A-Z][A-Z0-9]", cur) and re.fullmatch(r"\d{1,4}", nxt):
            merged.append(cur + nxt)
            i += 2
        else:
            merged.append(tokens[i])
            i += 1

    for token in merged:
        upper = token.upper()
        match = FLIGHT_RE.match(upper)
        if match and flight_no is None:
            flight_no = f"{match.group('carrier')}{int(match.group('num'))}"
            continue
        parsed = _parse_date_token(token, today)
        if parsed and day is None:
            day = parsed
            continue
        leftovers.append(token)

    if flight_no is None:
        raise QueryError(
            "편명을 인식하지 못했습니다. 예) `WE501`, `KE 86 내일`, `OZ102 20260908`"
        )
    day = day or today
    offset = (day - today).days
    if not (MIN_OFFSET <= offset <= MAX_OFFSET):
        raise QueryError(
            f"운항 현황 API는 조회일 기준 -3일 ~ +6일만 제공합니다. "
            f"({day:%Y-%m-%d} 은(는) {offset:+d}일)"
        )
    return FlightQuery(flight_no=flight_no, day=day)


def looks_like_flight_query(text: str) -> bool:
    """명령어가 아닌 일반 메시지가 편명 조회처럼 보이는지."""
    if not text or text.startswith("/"):
        return False
    return bool(re.search(r"\b[A-Z][A-Z0-9]\s*-?\s*\d{1,4}\b", text.upper()))
