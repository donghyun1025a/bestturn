"""공공데이터 응답을 봇 내부에서 쓰는 형태로 정규화."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

EMPTY_TOKENS = {"", "-", "없음", "null", "None", "N/A"}

# 응답 문서(표)와 실제 샘플 payload의 키 대소문자가 다릅니다(scheduleDatetime vs scheduleDateTime).
# 두 표기를 모두 받아들이기 위해 키를 소문자로 접어서 조회합니다.
def _pick(raw: dict[str, Any], *names: str) -> str | None:
    folded = {str(k).lower(): v for k, v in raw.items()}
    for name in names:
        value = folded.get(name.lower())
        if value is None:
            continue
        text = str(value).strip()
        if text not in EMPTY_TOKENS:
            return text
    return None


def parse_dt(value: str | None) -> datetime | None:
    """YYYYMMDDHH24MM (12자리) 문자열 파싱. 2400 같은 비정상 값도 방어."""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) < 12:
        return None
    digits = digits[:12]
    try:
        return datetime.strptime(digits, "%Y%m%d%H%M")
    except ValueError:
        # HHMM 이 2400 인 경우 등: 하루를 넘기지 않고 23:59 로 보정
        try:
            return datetime.strptime(digits[:8] + "2359", "%Y%m%d%H%M")
        except ValueError:
            return None


def normalize_flight_no(value: str | None) -> str:
    """'we 501', 'WE-0501' → 'WE501' (숫자 앞 0 제거)."""
    if not value:
        return ""
    text = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    m = re.match(r"^([A-Z0-9]{2,3}?)(\d{1,4})$", text)
    if not m:
        return text
    carrier, number = m.groups()
    return f"{carrier}{int(number)}"


@dataclass(frozen=True)
class Flight:
    """출발(OB)/도착(IB) 공통 항공편 정보."""

    direction: str  # "OB" | "IB"
    fid: str | None
    flight_id: str
    airline: str | None
    airport: str | None
    airport_code: str | None
    schedule_dt: datetime | None
    estimated_dt: datetime | None
    terminal_id: str | None
    gate_number: str | None
    checkin_range: str | None      # OB 전용
    carousel: str | None           # IB 전용
    exit_number: str | None        # IB 전용
    stand_position: str | None
    remark: str | None
    codeshare: str | None
    master_flight_id: str | None
    passenger_or_cargo: str | None
    aircraft_subtype: str | None
    aircraft_reg_no: str | None
    type_of_flight: str | None

    @property
    def is_inbound(self) -> bool:
        return self.direction == "IB"

    @property
    def carrier_code(self) -> str:
        m = re.match(r"^([A-Z0-9]{2,3}?)\d", self.flight_id or "")
        return m.group(1) if m else ""

    @property
    def key(self) -> str:
        """봇 내부 식별자. fid 가 없으면 편명+예정시각으로 대체."""
        if self.fid:
            return self.fid
        stamp = self.schedule_dt.strftime("%Y%m%d%H%M") if self.schedule_dt else "unknown"
        return f"{self.direction}:{normalize_flight_no(self.flight_id)}:{stamp}"

    @property
    def best_dt(self) -> datetime | None:
        return self.estimated_dt or self.schedule_dt

    @property
    def is_delayed(self) -> bool:
        if not (self.schedule_dt and self.estimated_dt):
            return False
        return self.estimated_dt > self.schedule_dt

    @classmethod
    def from_raw(cls, raw: dict[str, Any], direction: str) -> "Flight":
        return cls(
            direction=direction,
            fid=_pick(raw, "fid"),
            flight_id=(_pick(raw, "flightId") or "").upper(),
            airline=_pick(raw, "airline"),
            airport=_pick(raw, "airport"),
            airport_code=_pick(raw, "airportCode"),
            schedule_dt=parse_dt(_pick(raw, "scheduleDatetime", "scheduleDateTime")),
            estimated_dt=parse_dt(_pick(raw, "estimatedDatetime", "estimatedDateTime")),
            terminal_id=_pick(raw, "terminalId"),
            gate_number=_pick(raw, "gateNumber"),
            checkin_range=_pick(raw, "chkinRange", "checkinRange"),
            carousel=_pick(raw, "carousel"),
            exit_number=_pick(raw, "exitNumber"),
            stand_position=_pick(raw, "fstandPosition"),
            remark=_pick(raw, "remark"),
            codeshare=_pick(raw, "codeshare"),
            master_flight_id=_pick(raw, "masterFlightId"),
            passenger_or_cargo=_pick(raw, "passengerOrCargo"),
            aircraft_subtype=_pick(raw, "aircraftSubtype", "aircraftSubType"),
            aircraft_reg_no=_pick(raw, "aircraftRegNo"),
            type_of_flight=_pick(raw, "typeOfFlight"),
        )

    def watched_fields(self) -> dict[str, str | None]:
        """book 감시 대상 필드 (변동 시 알림)."""
        common: dict[str, str | None] = {
            "현황": self.remark,
            "예정시각": self.schedule_dt.strftime("%H:%M") if self.schedule_dt else None,
            "변경시각": self.estimated_dt.strftime("%H:%M") if self.estimated_dt else None,
            "터미널": self.terminal_id,
            "주기장": self.stand_position,
        }
        if self.is_inbound:
            common.update(
                {
                    "도착 게이트": self.gate_number,
                    "수하물 수취대": self.carousel,
                    "입국장 출구": self.exit_number,
                }
            )
        else:
            common.update({"체크인 카운터": self.checkin_range, "탑승구": self.gate_number})
        return common


@dataclass(frozen=True)
class Congestion:
    """출국장(보안검색 진입구) 실시간 혼잡도."""

    terminal_id: str | None
    gate_id: str
    wait_time_min: int | None
    wait_time_raw: str | None
    wait_length: int | None
    occur_time: datetime | None
    operating_time: str | None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Congestion":
        wait_raw = _pick(raw, "waitTime")
        wait_min: int | None = None
        if wait_raw:
            digits = re.sub(r"\D", "", wait_raw)
            if digits:
                wait_min = int(digits)
        length_raw = _pick(raw, "waitLength")
        occur = _pick(raw, "occurtime", "occurTime")
        occur_dt = None
        if occur:
            digits = re.sub(r"\D", "", occur)[:12]
            if len(digits) == 12:
                occur_dt = parse_dt(digits)
        return cls(
            terminal_id=_pick(raw, "terminalId"),
            gate_id=(_pick(raw, "gateId") or "").upper(),
            wait_time_min=wait_min,
            wait_time_raw=wait_raw,
            wait_length=int(re.sub(r"\D", "", length_raw)) if length_raw and re.sub(r"\D", "", length_raw) else None,
            occur_time=occur_dt,
            operating_time=_pick(raw, "operatingTime"),
        )

    @property
    def is_open(self) -> bool:
        return self.wait_time_min is not None


def dedupe_flights(flights: Iterable[Flight]) -> list[Flight]:
    seen: dict[str, Flight] = {}
    for flight in flights:
        seen.setdefault(flight.key, flight)
    return list(seen.values())
