"""UI 에서 켜고 끄는 백그라운드 수집 스레드."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .client import FirehoseClient
from .ingest import PITR_SAVE_EVERY
from .protocol import accepted_prefixes, airport_icao, normalize
from .settings import Settings
from .storage import EtaStore

log = logging.getLogger(__name__)


class IngestRunner:
    """스레드 하나로 스트림을 받아 적재하고, 진행 상황을 UI 에 노출합니다."""

    def __init__(self, store: EtaStore, build_client: Callable[[Settings], FirehoseClient] | None = None) -> None:
        self._store = store
        self._build_client = build_client or self._default_client
        self._thread: threading.Thread | None = None
        self._client: FirehoseClient | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.messages = 0
        self.stored = 0
        self.started_at: float | None = None
        self.last_message_at: float | None = None
        self.last_error: str | None = None

    @staticmethod
    def _default_client(settings: Settings) -> FirehoseClient:
        return FirehoseClient(
            username=settings.username,
            password=settings.password,
            host=settings.host,
            port=settings.port,
            airport=settings.airport,
        )

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        return {
            "running": self.running,
            "messages": self.messages,
            "stored": self.stored,
            "started_at": self.started_at,
            "last_message_at": self.last_message_at,
            "last_error": self.last_error,
            "connect_error": getattr(self._client, "last_connect_error", None),
            "pitr": self._store.get_pitr(),
        }

    def start(self, settings: Settings) -> bool:
        with self._lock:
            if self.running:
                return False
            self._stop.clear()
            self.last_error = None
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._run, args=(settings,), daemon=True)
            self._thread.start()
            return True

    def stop(self) -> bool:
        with self._lock:
            if not self.running:
                return False
            self._stop.set()
            return True

    def _run(self, settings: Settings) -> None:
        prefixes = accepted_prefixes(settings.airlines)
        dest = airport_icao(settings.airport)
        client = self._build_client(settings)
        client.last_pitr = self._store.get_pitr()
        self._client = client

        try:
            for message in client.stream():
                if self._stop.is_set():
                    break
                self.messages += 1
                self.last_message_at = time.time()

                update = normalize(message)
                if update is None:
                    continue
                if update.fields.get("dest") not in (None, dest) or update.airline not in prefixes:
                    continue
                self._store.apply(update)
                self.stored += 1
                if update.pitr and self.stored % PITR_SAVE_EVERY == 0:
                    self._store.set_pitr(update.pitr)
        except Exception as exc:  # UI 가 원인을 보여줄 수 있게 남깁니다
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("수집 스레드가 중단되었습니다")
