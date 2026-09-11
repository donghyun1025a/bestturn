"""Firehose 도착 예정시각과 인천공항 공공데이터를 같은 편끼리 맞춰 봅니다."""
from __future__ import annotations

from .iia import IiaArrival, normalize_reg
from .protocol import candidate_flight_numbers


def _index(arrivals: list[IiaArrival]) -> tuple[dict[str, IiaArrival], dict[str, IiaArrival]]:
    by_reg: dict[str, IiaArrival] = {}
    by_flight_no: dict[str, IiaArrival] = {}
    for arrival in arrivals:
        if arrival.reg:
            by_reg.setdefault(arrival.reg, arrival)
        for number in (arrival.flight_no, arrival.master_flight_no):
            if number:
                by_flight_no.setdefault(number, arrival)
    return by_reg, by_flight_no


def _match(row: dict, by_reg: dict[str, IiaArrival], by_flight_no: dict[str, IiaArrival]) -> tuple[IiaArrival | None, str | None]:
    # 기재 등록번호가 가장 확실합니다. 편명은 코드셰어·ICAO 표기 때문에 후보가 여럿입니다.
    reg = normalize_reg(row.get("reg"))
    if reg and reg in by_reg:
        return by_reg[reg], "reg"
    for number in sorted(candidate_flight_numbers(row.get("ident") or "")):
        if number in by_flight_no:
            return by_flight_no[number], "flight_no"
    return None, None


def compare(rows: list[dict], arrivals: list[IiaArrival]) -> list[dict]:
    """Firehose 편 목록 각각에 대조 결과를 붙여 돌려줍니다."""
    by_reg, by_flight_no = _index(arrivals)
    results = []
    for row in rows:
        arrival, matched_by = _match(row, by_reg, by_flight_no)
        firehose_eta = row.get("eta")
        iia_eta = (arrival.estimated or arrival.scheduled) if arrival else None
        diff = None
        if isinstance(firehose_eta, int) and isinstance(iia_eta, int):
            diff = round((firehose_eta - iia_eta) / 60)
        results.append(
            {
                "flight_id": row.get("flight_id"),
                "ident": row.get("ident"),
                "reg": row.get("reg"),
                "orig": row.get("orig"),
                "firehose": {
                    "eta": firehose_eta,
                    "eta_source": row.get("eta_source"),
                    "scheduled": row.get("scheduled_on") or row.get("scheduled_in"),
                    "gate": row.get("estimated_arrival_gate") or row.get("actual_arrival_gate"),
                    "carousel": row.get("baggage_claim"),
                    "cancelled": bool(row.get("cancelled")),
                },
                "iia": arrival.as_dict() if arrival else None,
                "matched_by": matched_by,
                "diff_minutes": diff,
            }
        )
    return results
