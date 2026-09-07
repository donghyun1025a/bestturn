from pathlib import Path

import pytest

from incheon_bot.domain.lounges import LoungeConfig
from incheon_bot.domain.routing import RoutingConfig

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture(scope="session")
def routing() -> RoutingConfig:
    return RoutingConfig(CONFIG_DIR / "routing.yml")


@pytest.fixture(scope="session")
def lounges() -> LoungeConfig:
    return LoungeConfig(CONFIG_DIR / "lounges.yml")


def departure_row(**overrides):
    row = {
        "aircraftRegNo": "HL8210",
        "aircraftSubType": "333",
        "airline": "타이스마일",
        "airport": "방콕/수완나품",
        "airportCode": "BKK",
        "chkinRange": "A-B",
        "codeshare": "Master",
        "estimatedDateTime": "202609071815",
        "fid": "2026090700000006269075",
        "flightId": "WE501",
        "gateNumber": "11",
        "passengerOrCargo": "Passenger",
        "remark": "-",
        "scheduleDateTime": "202609071800",
        "terminalId": "P01",
        "typeOfFlight": "I",
    }
    row.update(overrides)
    return row


def arrival_row(**overrides):
    row = {
        "aircraftSubtype": "32N",
        "airline": "에어아시아 버하드",
        "airport": "코타키나발루",
        "airportCode": "BKI",
        "carousel": "7",
        "codeshare": "Master",
        "estimatedDatetime": "202609071230",
        "exitNumber": "B",
        "fid": "2026090700000006268754",
        "flightId": "AK1623",
        "gateNumber": "244",
        "passengerOrCargo": "Passenger",
        "remark": "도착",
        "scheduleDatetime": "202609071215",
        "terminalId": "P03",
        "typeOfFlight": "I",
    }
    row.update(overrides)
    return row


def congestion_row(gate_id, wait, terminal="P01", **overrides):
    row = {
        "gateId": gate_id,
        "waitTime": str(wait),
        "waitLength": "20",
        "occurtime": "202609071649",
        "terminalId": terminal,
        "operatingTime": "05:00~22:00",
    }
    row.update(overrides)
    return row


def envelope(items, total=None):
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
            "body": {
                "items": items,
                "numOfRows": len(items),
                "pageNo": 1,
                "totalCount": len(items) if total is None else total,
            },
        }
    }
