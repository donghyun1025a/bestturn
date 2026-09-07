"""공공데이터포털(인천국제공항공사) OpenAPI 클라이언트.

- 항공기 운항 현황 상세 조회: /B551177/statusOfAllFltDeOdp
- 출국장 혼잡도(T1):        /B551177/statusOfDepartureCongestion
- 출국장 혼잡도(T2):        /B551177/statusOfDepartureCongestionT2

일일 트래픽(운항 500/오퍼레이션, 혼잡도 1000)을 넘기지 않도록
TTL 캐시 + 일일 호출 버짓을 내장합니다.
"""
from __future__ import annotations

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

import httpx

from .models import Congestion, Flight, normalize_flight_no

log = logging.getLogger(__name__)

FLIGHT_BASE = "https://apis.data.go.kr/B551177/statusOfAllFltDeOdp"
CONGESTION_T1 = "https://apis.data.go.kr/B551177/statusOfDepartureCongestion/getDepartureCongestion"
CONGESTION_T2 = "https://apis.data.go.kr/B551177/statusOfDepartureCongestionT2/getDepartureCongestionT2"

OPERATION = {"IB": "getFltArrivalsDeOdp", "OB": "getFltDeparturesDeOdp"}

FLIGHT_TTL = 60.0        # 항공편 목록 캐시 (초)
CONGESTION_TTL = 60.0    # 혼잡도는 1분 주기 갱신


class ApiError(RuntimeError):
    """API 호출 실패(인증/한도/서버 오류 등)."""


class BudgetExceeded(ApiError):
    """일일 호출 버짓 소진."""


@dataclass
class _CacheEntry:
    expires_at: float
    value: Any


class _DailyBudget:
    """자정(KST 서버 로컬 기준)에 초기화되는 단순 호출 카운터."""

    def __init__(self, limit: int, label: str) -> None:
        self.limit = limit
        self.label = label
        self._day = date.today()
        self._used = 0

    def _roll(self) -> None:
        today = date.today()
        if today != self._day:
            self._day = today
            self._used = 0

    def take(self) -> None:
        self._roll()
        if self._used >= self.limit:
            raise BudgetExceeded(
                f"{self.label} 일일 호출 한도({self.limit}회)를 모두 사용했습니다. 자정 이후 재시도해 주세요."
            )
        self._used += 1

    @property
    def used(self) -> int:
        self._roll()
        return self._used

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


def _unwrap(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """{response:{header,body:{items:...}}} 구조에서 item 목록과 totalCount 추출."""
    response = payload.get("response", payload) if isinstance(payload, dict) else {}
    header = response.get("header") or {}
    code = str(header.get("resultCode", "")).strip()
    if code and code not in {"00", "0", "000"}:
        raise ApiError(f"[{code}] {header.get('resultMsg', '알 수 없는 오류')}")

    body = response.get("body") or {}
    items = body.get("items")
    if items in (None, "", []):
        rows: list[Any] = []
    elif isinstance(items, dict):
        inner = items.get("item", [])
        rows = inner if isinstance(inner, list) else [inner]
    elif isinstance(items, list):
        rows = items
    else:
        rows = []
    total = body.get("totalCount")
    try:
        total_count = int(total)
    except (TypeError, ValueError):
        total_count = len(rows)
    return [r for r in rows if isinstance(r, dict)], total_count


def _unwrap_xml(text: str) -> tuple[list[dict[str, Any]], int]:
    root = ET.fromstring(text)
    code_el = root.find(".//resultCode")
    if code_el is not None and (code_el.text or "").strip() not in {"00", "0", "000"}:
        msg = root.find(".//resultMsg")
        raise ApiError(f"[{(code_el.text or '').strip()}] {(msg.text if msg is not None else '') or '오류'}")
    rows = [{child.tag: (child.text or "") for child in item} for item in root.iter("item")]
    total_el = root.find(".//totalCount")
    try:
        total = int((total_el.text or "0").strip()) if total_el is not None else len(rows)
    except ValueError:
        total = len(rows)
    return rows, total


class IncheonAirportClient:
    def __init__(
        self,
        service_key: str,
        *,
        flight_daily_budget: int = 450,
        congestion_daily_budget: int = 900,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = service_key
        self._client = client or httpx.AsyncClient(
            timeout=timeout, headers={"Accept": "application/json"}
        )
        self._owns_client = client is None
        self._cache: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.flight_budget = _DailyBudget(flight_daily_budget, "항공편 운항현황 API")
        self.congestion_budget = _DailyBudget(congestion_daily_budget, "출국장 혼잡도 API")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ 내부
    def _cached(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry and entry.expires_at > time.monotonic():
            return entry.value
        self._cache.pop(key, None)
        return None

    def _store(self, key: str, value: Any, ttl: float) -> None:
        self._cache[key] = _CacheEntry(time.monotonic() + ttl, value)

    async def _request(self, url: str, params: dict[str, Any], budget: _DailyBudget) -> tuple[list[dict], int]:
        budget.take()
        query = {k: v for k, v in params.items() if v not in (None, "")}
        query["serviceKey"] = self._key
        query.setdefault("type", "json")
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.get(url, params=query)
            except httpx.HTTPError as exc:  # 네트워크 오류만 재시도
                last_exc = exc
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code >= 500:
                last_exc = ApiError(f"HTTP {resp.status_code}")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise ApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            text = resp.text.strip()
            # 포털 오류는 JSON/XML 이 아닌 평문(text)으로 반환됩니다.
            if not text:
                raise ApiError("빈 응답을 받았습니다.")
            if text.startswith("{") or text.startswith("["):
                return _unwrap(resp.json())
            if text.startswith("<"):
                return _unwrap_xml(text)
            raise ApiError(f"API 오류: {text[:200]}")
        raise ApiError(f"API 호출에 실패했습니다: {last_exc}")

    # ------------------------------------------------------------- 항공편 조회
    async def fetch_flights(
        self,
        direction: str,
        search_date: str,
        *,
        flight_id: str | None = None,
        fid: str | None = None,
        passenger_or_cargo: str = "P",
        num_of_rows: int = 500,
    ) -> list[Flight]:
        """특정 일자의 출발/도착 편 목록. flight_id/fid 로 서버측 필터 가능."""
        operation = OPERATION[direction]
        cache_key = f"flt:{direction}:{search_date}:{flight_id or ''}:{fid or ''}:{passenger_or_cargo}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._cached(cache_key)
            if cached is not None:
                return cached
            rows, total = await self._request(
                f"{FLIGHT_BASE}/{operation}",
                {
                    "searchDate": search_date,
                    "searchdtCode": "E",
                    "searchFrom": "0000",
                    "searchTo": "2400",
                    "flightId": flight_id,
                    "fid": fid,
                    "passengerOrCargo": passenger_or_cargo,
                    "numOfRows": num_of_rows,
                    "pageNo": 1,
                },
                self.flight_budget,
            )
            # totalCount 가 1페이지를 넘으면 남은 페이지를 이어서 조회
            page = 2
            while len(rows) < total and page <= 10:
                more, _ = await self._request(
                    f"{FLIGHT_BASE}/{operation}",
                    {
                        "searchDate": search_date,
                        "searchdtCode": "E",
                        "searchFrom": "0000",
                        "searchTo": "2400",
                        "flightId": flight_id,
                        "fid": fid,
                        "passengerOrCargo": passenger_or_cargo,
                        "numOfRows": num_of_rows,
                        "pageNo": page,
                    },
                    self.flight_budget,
                )
                if not more:
                    break
                rows.extend(more)
                page += 1

            flights = [Flight.from_raw(row, direction) for row in rows]
            self._store(cache_key, flights, FLIGHT_TTL)
            return flights

    async def find_flight(
        self, flight_no: str, search_date: str, *, directions: Iterable[str] = ("IB", "OB")
    ) -> list[Flight]:
        """편명으로 도착/출발을 모두 조회해 IB/OB 를 자동 판별."""
        wanted = normalize_flight_no(flight_no)
        results: list[Flight] = []
        for direction in directions:
            try:
                flights = await self.fetch_flights(direction, search_date, flight_id=wanted)
            except BudgetExceeded:
                raise
            except ApiError as exc:
                log.warning("%s 조회 실패(%s): %s", direction, search_date, exc)
                continue
            matched = [f for f in flights if normalize_flight_no(f.flight_id) == wanted]
            if not matched:
                # 서버측 flightId 필터가 접두 일치 등으로 빗나가는 경우를 대비해 당일 전체에서 재탐색
                try:
                    whole_day = await self.fetch_flights(direction, search_date)
                except ApiError:
                    whole_day = []
                matched = [
                    f
                    for f in whole_day
                    if wanted in {normalize_flight_no(f.flight_id), normalize_flight_no(f.master_flight_id)}
                ]
            results.extend(matched)
        return results

    async def refresh_flight(self, flight: Flight, search_date: str) -> Flight | None:
        """book 감시용 단건 갱신. fid 가 있으면 fid 로, 없으면 편명으로 조회."""
        try:
            if flight.fid:
                flights = await self.fetch_flights(flight.direction, search_date, fid=flight.fid)
                for candidate in flights:
                    if candidate.fid == flight.fid:
                        return candidate
            matches = await self.find_flight(
                flight.flight_id, search_date, directions=(flight.direction,)
            )
        except ApiError:
            raise
        for candidate in matches:
            if candidate.key == flight.key or (flight.fid and candidate.fid == flight.fid):
                return candidate
        return matches[0] if matches else None

    # ------------------------------------------------------------ 혼잡도 조회
    async def fetch_congestion(self, terminal: str) -> list[Congestion]:
        """terminal: 'T1' | 'T2'."""
        terminal = terminal.upper()
        cache_key = f"cong:{terminal}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._cached(cache_key)
            if cached is not None:
                return cached
            url = CONGESTION_T2 if terminal == "T2" else CONGESTION_T1
            params: dict[str, Any] = {"numOfRows": 30, "pageNo": 1}
            if terminal == "T1":
                params["terminalId"] = "P01"
            rows, _ = await self._request(url, params, self.congestion_budget)
            data = [Congestion.from_raw(row) for row in rows]
            self._store(cache_key, data, CONGESTION_TTL)
            return data

    def budget_status(self) -> str:
        return (
            f"항공편 API {self.flight_budget.used}/{self.flight_budget.limit}회, "
            f"혼잡도 API {self.congestion_budget.used}/{self.congestion_budget.limit}회 사용"
        )
