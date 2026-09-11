"""인천국제공항공사 공공데이터 도착편 조회 (대조용).

봇(`src/incheon_bot/`)과 같은 엔드포인트를 쓰지만 이 프로젝트는 동기 호출이라
클라이언트를 공유하지 않습니다. 일일 한도(오퍼레이션당 500)를 지키려고 TTL 캐시를 둡니다.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

ARRIVALS_URL = "https://apis.data.go.kr/B551177/statusOfAllFltDeOdp/getFltArrivalsDeOdp"
KST = timezone(timedelta(hours=9))
CACHE_TTL = 60.0
EMPTY_TOKENS = {"", "-", "없음", "null", "None", "N/A"}


class IiaError(RuntimeError):
    """공공데이터 API 호출 실패."""


def _pick(raw: dict[str, Any], *names: str) -> str | None:
    # 명세표와 실제 payload 의 키 대소문자가 다릅니다 (scheduleDatetime vs scheduleDateTime).
    folded = {str(k).lower(): v for k, v in raw.items()}
    for name in names:
        value = folded.get(name.lower())
        if value is None:
            continue
        text = str(value).strip()
        if text not in EMPTY_TOKENS:
            return text
    return None


def _epoch(value: str | None) -> int | None:
    """YYYYMMDDHHMM (KST) → POSIX epoch. Firehose 와 같은 단위로 맞춥니다."""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)[:12]
    if len(digits) < 12:
        return None
    try:
        parsed = datetime.strptime(digits, "%Y%m%d%H%M")
    except ValueError:
        try:
            parsed = datetime.strptime(digits[:8] + "2359", "%Y%m%d%H%M")
        except ValueError:
            return None
    return int(parsed.replace(tzinfo=KST).timestamp())


def normalize_flight_no(value: str | None) -> str:
    """'we 501', 'WE-0501' → 'WE501' (숫자 앞 0 제거)."""
    if not value:
        return ""
    text = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    match = re.match(r"^([A-Z0-9]{2,3}?)(\d{1,4})$", text)
    if not match:
        return text
    carrier, number = match.groups()
    return f"{carrier}{int(number)}"


def normalize_reg(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


@dataclass(frozen=True)
class IiaArrival:
    flight_no: str
    master_flight_no: str
    airline: str | None
    origin: str | None
    scheduled: int | None
    estimated: int | None
    gate: str | None
    carousel: str | None
    exit_number: str | None
    terminal: str | None
    reg: str
    remark: str | None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "IiaArrival":
        return cls(
            flight_no=normalize_flight_no(_pick(raw, "flightId")),
            master_flight_no=normalize_flight_no(_pick(raw, "masterFlightId")),
            airline=_pick(raw, "airline"),
            origin=_pick(raw, "airport"),
            scheduled=_epoch(_pick(raw, "scheduleDatetime", "scheduleDateTime")),
            estimated=_epoch(_pick(raw, "estimatedDatetime", "estimatedDateTime")),
            gate=_pick(raw, "gateNumber"),
            carousel=_pick(raw, "carousel"),
            exit_number=_pick(raw, "exitNumber"),
            terminal=_pick(raw, "terminalId"),
            reg=normalize_reg(_pick(raw, "aircraftRegNo")),
            remark=_pick(raw, "remark"),
        )

    def as_dict(self) -> dict:
        return {
            "flight_no": self.flight_no,
            "airline": self.airline,
            "origin": self.origin,
            "scheduled": self.scheduled,
            "estimated": self.estimated,
            "gate": self.gate,
            "carousel": self.carousel,
            "exit_number": self.exit_number,
            "terminal": self.terminal,
            "reg": self.reg,
            "remark": self.remark,
        }


def _rows(text: str, payload_json: Any) -> list[dict]:
    """포털은 JSON·XML·평문 오류를 모두 돌려줍니다."""
    if text.startswith("<"):
        root = ET.fromstring(text)
        code = root.find(".//resultCode")
        if code is not None and (code.text or "").strip() not in {"00", "0", "000"}:
            message = root.find(".//resultMsg")
            raise IiaError(f"[{(code.text or '').strip()}] {(message.text if message is not None else '') or '오류'}")
        return [{child.tag: (child.text or "") for child in item} for item in root.iter("item")]

    response = payload_json.get("response", payload_json) if isinstance(payload_json, dict) else {}
    header = response.get("header") or {}
    code = str(header.get("resultCode", "")).strip()
    if code and code not in {"00", "0", "000"}:
        raise IiaError(f"[{code}] {header.get('resultMsg', '알 수 없는 오류')}")
    items = (response.get("body") or {}).get("items")
    if isinstance(items, dict):
        inner = items.get("item", [])
        items = inner if isinstance(inner, list) else [inner]
    return [row for row in (items or []) if isinstance(row, dict)]


class IiaClient:
    def __init__(self, service_key: str, *, client: httpx.Client | None = None) -> None:
        self._key = service_key
        self._client = client or httpx.Client(timeout=15.0, headers={"Accept": "application/json"})
        self._cache: dict[str, tuple[float, list[IiaArrival]]] = {}

    def arrivals(self, search_date: str) -> list[IiaArrival]:
        """search_date: YYYYMMDD. 조회일 기준 -3 ~ +6일만 제공됩니다."""
        if not self._key:
            raise IiaError("공공데이터 서비스키가 설정되지 않았습니다.")
        cached = self._cache.get(search_date)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        response = self._client.get(
            ARRIVALS_URL,
            params={
                "serviceKey": self._key,
                "type": "json",
                "searchDate": search_date,
                "searchdtCode": "E",
                "searchFrom": "0000",
                "searchTo": "2400",
                "passengerOrCargo": "P",
                "numOfRows": 500,
                "pageNo": 1,
            },
        )
        if response.status_code != 200:
            raise IiaError(f"HTTP {response.status_code}: {response.text[:200]}")
        text = response.text.strip()
        if not text:
            raise IiaError("빈 응답을 받았습니다.")
        if not (text.startswith("{") or text.startswith("[") or text.startswith("<")):
            raise IiaError(f"API 오류: {text[:200]}")

        arrivals = [IiaArrival.from_raw(row) for row in _rows(text, response.json() if text[0] != "<" else None)]
        self._cache[search_date] = (time.monotonic() + CACHE_TTL, arrivals)
        return arrivals
