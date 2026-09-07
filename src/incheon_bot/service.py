"""항공편 1건에 대한 의전 브리핑 조립 (IB/OB 자동 분기)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .api.client import ApiError, IncheonAirportClient
from .api.models import Congestion, Flight
from .domain.lounges import LoungeConfig, LoungeSuggestion
from .domain.routing import RouteOption, RoutingConfig, Terminal

log = logging.getLogger(__name__)


@dataclass
class Briefing:
    flight: Flight
    terminal: Terminal | None
    # OB 전용
    route_options: list[RouteOption] = field(default_factory=list)
    route_origin: str = ""
    lounges: LoungeSuggestion | None = None
    # IB 전용
    immigration: str | None = None
    baggage_area: str | None = None
    transfer_note: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def is_inbound(self) -> bool:
        return self.flight.is_inbound

    @property
    def best_route(self) -> RouteOption | None:
        """도보 + 실시간 대기 합계가 가장 짧은 출국장."""
        for option in self.route_options:
            if option.has_data:
                return option
        return self.route_options[0] if self.route_options else None

    @property
    def nearest_route(self) -> RouteOption | None:
        """혼잡도와 무관하게 물리적으로 가장 가까운(운영 중인) 출국장."""
        by_walk = sorted(
            (o for o in self.route_options if o.has_data), key=lambda o: o.walk_minutes
        )
        return by_walk[0] if by_walk else None

    def nearby(self, limit: int = 4) -> list[RouteOption]:
        """추천 출국장 주변의 가까운 출국장(거리순)."""
        best = self.best_route
        by_walk = sorted(
            (o for o in self.route_options if o is not best), key=lambda o: o.walk_minutes
        )
        return by_walk[:limit]


class BriefingService:
    def __init__(self, client: IncheonAirportClient, config_dir: Path) -> None:
        self.client = client
        self.config_dir = Path(config_dir)
        self.routing = RoutingConfig(self.config_dir / "routing.yml")
        self.lounges = LoungeConfig(self.config_dir / "lounges.yml")

    def reload_config(self) -> None:
        self.routing.reload()
        self.lounges.reload()

    async def search(self, flight_no: str, search_date: str) -> list[Flight]:
        flights = await self.client.find_flight(flight_no, search_date)
        flights.sort(key=lambda f: (f.best_dt is None, f.best_dt or 0, f.direction))
        return flights

    async def congestion_for(self, terminal_code: str | None) -> tuple[list[Congestion], str | None]:
        term, api = self.routing.congestion_terminal(terminal_code)
        if term is None or not api:
            return [], None
        try:
            return await self.client.fetch_congestion(api), None
        except ApiError as exc:
            log.warning("혼잡도 조회 실패(%s): %s", api, exc)
            return [], f"실시간 혼잡도를 불러오지 못했습니다: {exc}"

    async def build(self, flight: Flight) -> Briefing:
        terminal = self.routing.terminal(flight.terminal_id)
        briefing = Briefing(flight=flight, terminal=terminal)

        if terminal and terminal.cargo:
            briefing.warnings.append("화물터미널 운항편입니다. 여객 동선 정보가 제공되지 않습니다.")
            return briefing
        if terminal and terminal.transfer_note:
            briefing.transfer_note = terminal.transfer_note

        if flight.is_inbound:
            briefing.immigration = self.routing.immigration_for(flight.terminal_id, flight.gate_number)
            briefing.baggage_area = terminal.baggage_floor if terminal else None
            if not flight.carousel:
                briefing.warnings.append("수하물 수취대는 착륙 전후에 확정됩니다. /book 으로 자동 알림을 받으세요.")
            if not flight.exit_number:
                briefing.warnings.append("입국장 출구 번호가 아직 배정되지 않았습니다.")
            return briefing

        congestion, warning = await self.congestion_for(flight.terminal_id)
        if warning:
            briefing.warnings.append(warning)
        options, origin = self.routing.route_options(
            flight.terminal_id,
            congestion,
            gate_number=flight.gate_number,
            checkin_range=flight.checkin_range,
        )
        briefing.route_options = options
        briefing.route_origin = origin
        briefing.lounges = self.lounges.suggest(
            flight.carrier_code, (terminal.code if terminal else None)
        )
        if not flight.checkin_range:
            briefing.warnings.append("체크인 카운터는 통상 출발 3~4시간 전 배정됩니다. /book 으로 자동 알림을 받으세요.")
        if not flight.gate_number:
            briefing.warnings.append("탑승구는 아직 배정 전입니다. 카운터 위치 기준으로 동선을 계산했습니다.")
        return briefing
