"""Firehose 스트림을 받아 대상 항공편만 DB 에 적재합니다."""
from __future__ import annotations

import logging
from collections.abc import Iterable

from .client import FirehoseClient
from .protocol import accepted_prefixes, airport_icao, normalize
from .settings import Settings
from .storage import EtaStore

log = logging.getLogger(__name__)

# pitr 은 매 메시지마다 바뀌므로 이 간격으로만 저장합니다.
PITR_SAVE_EVERY = 100


def ingest(messages: Iterable[dict], store: EtaStore, settings: Settings) -> int:
    """대상 편의 메시지를 적재하고 처리 건수를 돌려줍니다."""
    prefixes = accepted_prefixes(settings.airlines)
    dest = airport_icao(settings.airport)
    seen = 0

    for message in messages:
        update = normalize(message)
        if update is None:
            continue
        # airport_filter 는 출발·도착을 모두 통과시키므로 도착편만 다시 거릅니다.
        if update.fields.get("dest") not in (None, dest):
            continue
        if update.airline not in prefixes:
            continue

        changed = store.apply(update)
        seen += 1
        if changed:
            log.info("%s ETA 변경 → %s", update.ident, update.best_eta())
        if update.pitr and seen % PITR_SAVE_EVERY == 0:
            store.set_pitr(update.pitr)
    return seen


def run(settings: Settings) -> None:
    settings.validate()
    store = EtaStore(settings.db_path)
    client = FirehoseClient(
        username=settings.username,
        password=settings.password,
        host=settings.host,
        port=settings.port,
        airport=settings.airport,
        ca_bundle=settings.ca_bundle or None,
    )
    client.last_pitr = store.get_pitr()
    ingest(client.stream(), store, settings)
