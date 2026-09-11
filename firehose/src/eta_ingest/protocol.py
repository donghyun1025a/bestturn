"""Firehose 초기화 명령 생성과 메시지 정규화.

Firehose 는 같은 값을 메시지 종류에 따라 다른 이름으로 보냅니다
(flightplan/position 은 `eta`, flifo 는 `estimated_on`). 여기서 한 이름으로 모읍니다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ICAO_BY_IATA: dict[str, tuple[str, ...]] = {
    "WE": ("THA", "THD"),  # Thai Airways / Thai Smile
    "8M": ("MMA",),        # Myanmar Airways International
    "AS": ("ASA",),        # Alaska Airlines
    "AA": ("AAL",),        # American Airlines
    "WS": ("WJA",),        # WestJet
}

ICAO_BY_AIRPORT: dict[str, str] = {"ICN": "RKSI", "GMP": "RKSS", "CJU": "RKPC", "PUS": "RKPK"}

# 도착 예측에 필요한 이벤트만 구독합니다.
DEFAULT_EVENTS = ("flifo", "arrival", "departure", "cancellation", "position")

# flifo 의 긴 이름을 기준으로 축약형을 흡수합니다.
ALIASES = {
    "adt": "actual_off",
    "aat": "actual_on",
    "edt": "estimated_off",
    "eta": "estimated_on",
    "fdt": "scheduled_off",
    "ete": "en_route_time",
}

EPOCH_FIELDS = (
    "scheduled_out", "scheduled_off", "scheduled_on", "scheduled_in",
    "estimated_out", "estimated_off", "estimated_on", "estimated_in",
    "predicted_out", "predicted_off", "predicted_on", "predicted_in",
    "actual_out", "actual_off", "actual_on", "actual_in",
)

TEXT_FIELDS = (
    "ident", "reg", "aircrafttype", "orig", "dest", "status",
    "actual_arrival_gate", "estimated_arrival_gate",
    "actual_arrival_terminal", "scheduled_arrival_terminal",
    "actual_runway_on", "baggage_claim",
)

# 도착 예정시각으로 삼을 값의 우선순위. 앞의 값이 있으면 그것을 씁니다.
ETA_PRIORITY = ("actual_in", "predicted_in", "estimated_in", "actual_on", "predicted_on", "estimated_on")

# 편명은 코드 + 숫자(+ 선택적 접미 문자) 전체가 맞아떨어져야 합니다.
# 그래야 기체 등록번호(N104BA, CGEYQ)를 항공사 코드로 오인하지 않습니다.
_ICAO_IDENT = re.compile(r"^([A-Z]{3})(\d{1,4}[A-Z]?)$")
_IATA_IDENT = re.compile(r"^([A-Z]{2}|[A-Z]\d|\d[A-Z])(\d{1,4}[A-Z]?)$")


def airport_icao(code: str) -> str:
    code = code.strip().upper()
    return ICAO_BY_AIRPORT.get(code, code)


def accepted_prefixes(airlines: tuple[str, ...]) -> frozenset[str]:
    """IATA 코드 목록을 ident 앞자리로 쓸 수 있는 IATA·ICAO 집합으로 펼칩니다."""
    out: set[str] = set()
    for code in airlines:
        code = code.strip().upper()
        if not code:
            continue
        out.add(code)
        out.update(ICAO_BY_IATA.get(code, ()))
    return frozenset(out)


def ident_airline(ident: str) -> str | None:
    """편명에서 항공사 코드를 뽑습니다. THA501 → THA, 8M501 → 8M."""
    ident = (ident or "").strip().upper()
    for pattern in (_ICAO_IDENT, _IATA_IDENT):
        match = pattern.match(ident)
        if match:
            return match.group(1)
    return None


def init_command(
    *,
    username: str,
    password: str,
    time_mode: str = "live",
    airport: str | None = None,
    events: tuple[str, ...] = DEFAULT_EVENTS,
    useragent: str = "bestturn-eta",
    keepalive: int = 60,
    compression: str | None = "gzip",
) -> str:
    """Firehose 접속 직후 보낼 한 줄 명령. 개행까지 포함해 돌려줍니다.

    time_mode 는 `live` 또는 `pitr <epoch>` 입니다 (재접속 시 무손실 재개).
    """
    parts = [time_mode, "username", username, "password", password, "useragent", useragent]
    if keepalive:
        parts += ["keepalive", str(keepalive)]
    if compression:
        parts += ["compression", compression]
    if events:
        parts += ["events", f'"{" ".join(events)}"']
    if airport:
        # orig 또는 dest 가 일치하면 통과합니다. 도착편 선별은 수신 후 dest 로 한 번 더 합니다.
        parts += ["airport_filter", f'"{airport_icao(airport)}"']
    return " ".join(parts) + "\n"


def _epoch(value: object) -> int | None:
    """Firehose 는 모든 스칼라를 문자열로 보냅니다."""
    if value in (None, "", "0"):
        return None
    try:
        parsed = int(str(value))
    except ValueError:
        return None
    return parsed or None


@dataclass(frozen=True)
class Update:
    """한 메시지에서 뽑아낸, 한 항공편의 변경분."""

    flight_id: str
    ident: str
    msg_type: str
    pitr: int | None
    fields: dict[str, object]

    @property
    def airline(self) -> str | None:
        return ident_airline(self.ident)

    def best_eta(self) -> tuple[str, int] | None:
        for name in ETA_PRIORITY:
            value = self.fields.get(name)
            if isinstance(value, int):
                return name, value
        return None


def normalize(message: dict) -> Update | None:
    """Firehose 메시지 한 건을 Update 로 변환합니다. 대상이 아니면 None."""
    flight_id = str(message.get("id") or "").strip()
    msg_type = str(message.get("type") or "").strip()
    if not flight_id or not msg_type:
        return None

    raw: dict[str, object] = dict(message)
    for short, long in ALIASES.items():
        if short in raw and long not in raw:
            raw[long] = raw.pop(short)

    # offblock/onblock 은 시각을 clock 으로만 보냅니다.
    clock = _epoch(raw.get("clock"))
    if msg_type == "onblock" and clock:
        raw["actual_in"] = clock
    elif msg_type == "offblock" and clock:
        raw["actual_out"] = clock

    fields: dict[str, object] = {}
    for name in EPOCH_FIELDS:
        value = _epoch(raw.get(name))
        if value is not None:
            fields[name] = value
    for name in TEXT_FIELDS:
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            fields[name] = value.strip()
    if msg_type == "cancellation" or str(raw.get("trueCancel") or "") == "1":
        fields["cancelled"] = 1

    return Update(
        flight_id=flight_id,
        ident=str(raw.get("ident") or "").strip().upper(),
        msg_type=msg_type,
        pitr=_epoch(raw.get("pitr")),
        fields=fields,
    )
