"""Firehose 스트리밍 클라이언트 (TLS 소켓 + 개행 구분 JSON)."""
from __future__ import annotations

import json
import logging
import socket
import ssl
import time
import zlib
from collections.abc import Iterator
from typing import Callable

from .protocol import init_command

log = logging.getLogger(__name__)

# 압축 방식별 zlib 윈도우 크기. 초기화 명령 자체는 압축하지 않고 보냅니다.
WBITS = {"deflate": -zlib.MAX_WBITS, "compress": zlib.MAX_WBITS, "gzip": 16 | zlib.MAX_WBITS}

READ_TIMEOUT_MARGIN = 10
# keepalive 의 pitr 이 이만큼 연속으로 제자리면 스트림이 멈춘 것으로 보고 재접속합니다.
STALE_KEEPALIVE_LIMIT = 5
BACKOFF_SECONDS = (2, 4, 8, 16, 30)


class FirehoseError(RuntimeError):
    """서버가 error 메시지를 보냈거나 접속이 끊긴 경우."""


def _connect(host: str, port: int, timeout: float) -> ssl.SSLSocket:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    raw = socket.create_connection((host, port), timeout=timeout)
    sock = context.wrap_socket(raw, server_hostname=host)
    sock.settimeout(timeout)
    return sock


class FirehoseClient:
    def __init__(
        self,
        *,
        username: str,
        password: str,
        host: str = "firehose.flightaware.com",
        port: int = 1501,
        airport: str | None = None,
        keepalive: int = 60,
        compression: str | None = "gzip",
        connect: Callable[[str, int, float], ssl.SSLSocket] = _connect,
    ) -> None:
        self.username = username
        self.password = password
        self.host = host
        self.port = port
        self.airport = airport
        self.keepalive = keepalive
        self.compression = compression
        self._connect = connect
        self.last_pitr: int | None = None

    def _session(self, sock: ssl.SSLSocket) -> Iterator[dict]:
        """접속 1회분. 끊기면 반환하고, 서버 오류면 FirehoseError 를 냅니다."""
        time_mode = f"pitr {self.last_pitr}" if self.last_pitr else "live"
        command = init_command(
            username=self.username,
            password=self.password,
            time_mode=time_mode,
            airport=self.airport,
            keepalive=self.keepalive,
            compression=self.compression,
        )
        log.info("Firehose 접속 — %s", time_mode)
        sock.sendall(command.encode())

        decompressor = zlib.decompressobj(WBITS[self.compression]) if self.compression else None
        buffer = b""
        stale = 0
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                log.info("서버가 연결을 종료했습니다")
                return
            if decompressor is not None:
                chunk = decompressor.decompress(chunk)
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("JSON 파싱 실패, 건너뜁니다 (%d bytes)", len(line))
                    continue

                pitr = message.get("pitr")
                if message.get("type") == "keepalive":
                    stale = stale + 1 if str(pitr) == str(self.last_pitr) else 0
                if pitr:
                    self.last_pitr = int(pitr)
                if message.get("type") == "error":
                    raise FirehoseError(str(message.get("error_msg") or "unknown error"))
                if stale >= STALE_KEEPALIVE_LIMIT:
                    log.warning("pitr 이 %d 회 연속 멈춰 재접속합니다", stale)
                    return
                yield message

    def stream(self, *, max_failures: int = 3) -> Iterator[dict]:
        """끊기면 마지막 pitr 로 재개하며 무한히 메시지를 내보냅니다."""
        failures = 0
        while True:
            sock = None
            try:
                sock = self._connect(self.host, self.port, self.keepalive + READ_TIMEOUT_MARGIN)
                yield from self._session(sock)
                failures = 0
            except (OSError, FirehoseError) as exc:
                # pitr 을 한 번도 못 받았다면 재개할 지점이 없으므로 실패를 셉니다.
                if self.last_pitr is None:
                    failures += 1
                    if failures >= max_failures:
                        raise
                log.warning("접속 실패 (%s), 재접속합니다", exc)
            finally:
                if sock is not None:
                    sock.close()
            time.sleep(BACKOFF_SECONDS[min(failures, len(BACKOFF_SECONDS) - 1)])
