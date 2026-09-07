"""항공사 → 얼라이언스 → 이용 가능 라운지 매핑 (수동 설정 파일 기반)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Lounge:
    key: str
    name: str
    terminal: str | None
    location: str
    hours: str
    access: str


@dataclass(frozen=True)
class LoungeSuggestion:
    alliance_name: str | None
    alliance_lounges: tuple[Lounge, ...]
    airline_lounges: tuple[Lounge, ...]
    contract_lounges: tuple[Lounge, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.alliance_lounges or self.airline_lounges or self.contract_lounges)


class LoungeConfig:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.reload()

    def reload(self) -> None:
        data: dict[str, Any] = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self._lounges = {
            key: Lounge(
                key=key,
                name=raw.get("name", key),
                terminal=(raw.get("terminal") or None),
                location=raw.get("location", "-"),
                hours=raw.get("hours", "-"),
                access=raw.get("access", "-"),
            )
            for key, raw in (data.get("lounges") or {}).items()
        }
        self._alliances = data.get("alliances") or {}
        self._airline_to_alliance: dict[str, str] = {}
        for alliance_key, raw in self._alliances.items():
            for code in raw.get("airlines") or []:
                self._airline_to_alliance[str(code).upper()] = alliance_key
        self._airline_overrides = {
            str(k).upper(): v for k, v in (data.get("airline_overrides") or {}).items()
        }
        self._alliance_lounges = data.get("alliance_lounges") or {}
        self._contract = data.get("contract_lounges") or []

    def _resolve(self, keys: Any, terminal: str | None) -> tuple[Lounge, ...]:
        out: list[Lounge] = []
        for key in keys or []:
            lounge = self._lounges.get(key)
            if lounge is None:
                continue
            if terminal and lounge.terminal and lounge.terminal != terminal:
                continue
            if lounge not in out:
                out.append(lounge)
        return tuple(out)

    def alliance_of(self, carrier_code: str | None) -> tuple[str | None, str | None]:
        code = (carrier_code or "").upper()
        key = self._airline_to_alliance.get(code)
        if not key:
            return None, None
        return key, (self._alliances.get(key) or {}).get("name", key)

    def suggest(self, carrier_code: str | None, terminal: str | None = None) -> LoungeSuggestion:
        """해당 항공사 승객이 이용 가능한 라운지. terminal 지정 시 해당 터미널만."""
        alliance_key, alliance_name = self.alliance_of(carrier_code)
        override = self._airline_overrides.get((carrier_code or "").upper(), {})
        return LoungeSuggestion(
            alliance_name=alliance_name,
            alliance_lounges=self._resolve(self._alliance_lounges.get(alliance_key), terminal),
            airline_lounges=self._resolve(override.get("lounges"), terminal),
            contract_lounges=self._resolve(self._contract, terminal),
        )

    def all_lounges(self, terminal: str | None = None) -> tuple[Lounge, ...]:
        return self._resolve(list(self._lounges), terminal)
