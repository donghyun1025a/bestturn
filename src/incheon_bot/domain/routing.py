"""터미널 동선 설정 로딩 + 출국장 최적 경로 추천."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..api.models import Congestion


@dataclass(frozen=True)
class DepartureGate:
    gate_id: str      # DG1_E 등 혼잡도 API 의 gateId
    label: str
    dg: str           # 출국장 번호
    pos: float


@dataclass(frozen=True)
class GateZone:
    lo: int
    hi: int
    pos: float
    immigration: str

    def contains(self, gate: int) -> bool:
        return self.lo <= gate <= self.hi


@dataclass(frozen=True)
class Terminal:
    code: str
    name: str
    short: str
    congestion_api: str | None
    departure_gates: dict[str, DepartureGate]
    checkin_counters: dict[str, float]
    gate_zones: tuple[GateZone, ...]
    default_immigration: str
    baggage_floor: str
    transfer_note: str | None
    extra_minutes: int
    parent: str | None
    cargo: bool


@dataclass(frozen=True)
class RouteOption:
    gate: DepartureGate
    congestion: Congestion | None
    walk_minutes: float

    @property
    def wait_minutes(self) -> int | None:
        return self.congestion.wait_time_min if self.congestion else None

    @property
    def total_minutes(self) -> float:
        """도보 + 대기. 데이터 없는 출국장은 비교에서 뒤로 밀리도록 큰 값."""
        if self.wait_minutes is None:
            return self.walk_minutes + 999
        return self.walk_minutes + self.wait_minutes

    @property
    def has_data(self) -> bool:
        return self.congestion is not None and self.congestion.is_open


def _parse_range(text: str) -> tuple[int, int]:
    nums = [int(n) for n in re.findall(r"\d+", str(text))]
    if not nums:
        return (0, 0)
    return (nums[0], nums[-1] if len(nums) > 1 else nums[0])


def gate_to_int(gate_number: str | None) -> int | None:
    if not gate_number:
        return None
    digits = re.sub(r"\D", "", gate_number)
    return int(digits) if digits else None


class RoutingConfig:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.reload()

    def reload(self) -> None:
        data: dict[str, Any] = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self.walk_per_unit: float = float(data.get("walk_minutes_per_unit", 0.12))
        self.levels: list[dict[str, Any]] = data.get("congestion_levels", [])
        terminals: dict[str, Terminal] = {}
        for code, raw in (data.get("terminals") or {}).items():
            raw = raw or {}
            gates = {
                gid: DepartureGate(gid, g.get("label", gid), str(g.get("dg", "")), float(g.get("pos", 50)))
                for gid, g in (raw.get("departure_gates") or {}).items()
            }
            zones = []
            for zone in raw.get("gate_zones") or []:
                lo, hi = _parse_range(zone.get("range", ""))
                zones.append(GateZone(lo, hi, float(zone.get("pos", 50)), zone.get("immigration", "")))
            terminals[code] = Terminal(
                code=code,
                name=raw.get("name", code),
                short=raw.get("short", code),
                congestion_api=raw.get("congestion_api"),
                departure_gates=gates,
                checkin_counters={k.upper(): float(v) for k, v in (raw.get("checkin_counters") or {}).items()},
                gate_zones=tuple(zones),
                default_immigration=raw.get("default_immigration", "입국심사장"),
                baggage_floor=raw.get("baggage_floor", "1층 수하물 수취지역"),
                transfer_note=raw.get("transfer_note"),
                extra_minutes=int(raw.get("extra_minutes", 0) or 0),
                parent=raw.get("parent"),
                cargo=bool(raw.get("cargo", False)),
            )
        self.terminals = terminals

    # ------------------------------------------------------------------ 조회
    def terminal(self, code: str | None) -> Terminal | None:
        return self.terminals.get((code or "").upper())

    def congestion_terminal(self, code: str | None) -> tuple[Terminal | None, str | None]:
        """혼잡도 조회에 사용할 터미널(탑승동은 T1 로 위임)과 API 구분자."""
        term = self.terminal(code)
        if term is None:
            return None, None
        if term.parent and not term.departure_gates:
            parent = self.terminal(term.parent)
            if parent:
                return parent, parent.congestion_api
        return term, term.congestion_api

    def level_for(self, wait_minutes: int | None) -> tuple[str, str]:
        """혼잡도 4단계 (원활/보통/혼잡/매우혼잡)."""
        if wait_minutes is None:
            return ("정보없음", "⚪")
        for level in self.levels:
            ceiling = level.get("max")
            if ceiling is None or wait_minutes < int(ceiling):
                return (level.get("name", "-"), level.get("emoji", ""))
        return ("정보없음", "⚪")

    def zone_for_gate(self, terminal: Terminal | None, gate_number: str | None) -> GateZone | None:
        if terminal is None:
            return None
        gate = gate_to_int(gate_number)
        if gate is None:
            return None
        for zone in terminal.gate_zones:
            if zone.contains(gate):
                return zone
        return None

    def immigration_for(self, terminal_code: str | None, gate_number: str | None) -> str:
        term = self.terminal(terminal_code)
        if term is None:
            return "입국심사장"
        zone = self.zone_for_gate(term, gate_number)
        return zone.immigration if zone and zone.immigration else term.default_immigration

    def origin_pos(
        self, terminal_code: str | None, gate_number: str | None, checkin_range: str | None
    ) -> tuple[float | None, str]:
        """출발 동선의 기준 위치. 체크인 카운터가 있으면 카운터, 없으면 탑승구 구역."""
        term = self.terminal(terminal_code)
        route_term, _ = self.congestion_terminal(terminal_code)
        counters = (route_term or term).checkin_counters if (route_term or term) else {}
        if checkin_range and counters:
            letters = {c for c in re.sub(r"[^A-Za-z]", "", checkin_range).upper() if c in counters}
            if letters:
                positions = [counters[c] for c in letters]
                return (sum(positions) / len(positions), f"체크인 카운터 {checkin_range}")
        zone = self.zone_for_gate(term, gate_number)
        if zone:
            return (zone.pos, f"탑승구 {gate_number}")
        return (None, "기준 위치 미확인")

    def route_options(
        self,
        terminal_code: str | None,
        congestion: list[Congestion],
        *,
        gate_number: str | None = None,
        checkin_range: str | None = None,
    ) -> tuple[list[RouteOption], str]:
        """가까운 순 + 실시간 대기시간을 합산해 정렬된 출국장 후보 반환."""
        route_term, _ = self.congestion_terminal(terminal_code)
        if route_term is None or not route_term.departure_gates:
            return [], "기준 위치 미확인"
        pos, origin_label = self.origin_pos(terminal_code, gate_number, checkin_range)
        by_id = {c.gate_id: c for c in congestion}
        options: list[RouteOption] = []
        for gate_id, gate in route_term.departure_gates.items():
            walk = 0.0 if pos is None else abs(gate.pos - pos) * self.walk_per_unit
            options.append(RouteOption(gate, by_id.get(gate_id), round(walk, 1)))
        options.sort(key=lambda o: (o.total_minutes, o.walk_minutes))
        return options, origin_label
