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
# firehose.flightaware.com 은 A 레코드가 8개입니다. socket.create_connection 은
# 주소마다 타임아웃을 처음부터 다시 쓰기 때문에, 막혀 있으면 8배로 멈춥니다.
# 전체 마감시간을 걸어 몇 초 안에 원인을 알 수 있게 합니다.
CONNECT_TIMEOUT = 15.0
PER_ADDRESS_TIMEOUT = 5.0
# keepalive 의 pitr 이 이만큼 연속으로 제자리면 스트림이 멈춘 것으로 보고 재접속합니다.
STALE_KEEPALIVE_LIMIT = 5
BACKOFF_SECONDS = (2, 4, 8, 16, 30)


class FirehoseError(RuntimeError):
    """서버가 error 메시지를 보냈거나 접속이 끊긴 경우."""


def build_context(ca_bundle: str | None = None) -> ssl.SSLContext:
    """검증에 쓸 TLS 설정.

    윈도우의 파이썬은 시스템 루트 저장소에 "아직 내려받지 않은" 루트 인증서를 보지 못해
    멀쩡한 사이트도 검증에 실패합니다. certifi 번들이 있으면 그것을 함께 신뢰합니다.
    사내 방화벽이 TLS 를 가로채는 환경에서는 FIREHOSE_CA_BUNDLE 로 사내 CA 를 지정하세요.
    """
    if ca_bundle:
        context = ssl.create_default_context(cafile=ca_bundle)
    else:
        context = ssl.create_default_context()
        try:
            import certifi

            context.load_verify_locations(cafile=certifi.where())
        except ImportError:
            pass
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def describe_failure(exc: Exception) -> str:
    """실패 사유를 화면에 그대로 띄울 수 있는 문장으로 바꿉니다."""
    if isinstance(exc, FirehoseError):
        return f"Firehose 가 오류를 보냈습니다: {exc}"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return (
            "TLS 인증서 검증 실패. 사내 방화벽이 통신을 가로채고 있을 수 있습니다. "
            "「python run.py doctor」 를 실행해 어느 쪽인지 확인하세요."
        )
    return str(exc)


def _open_socket(host: str, port: int) -> socket.socket:
    """주소를 차례로 시도하되 전체 CONNECT_TIMEOUT 을 넘기지 않습니다."""
    deadline = time.monotonic() + CONNECT_TIMEOUT
    failures: list[str] = []
    for family, kind, proto, _, address in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock = socket.socket(family, kind, proto)
        sock.settimeout(min(PER_ADDRESS_TIMEOUT, remaining))
        try:
            sock.connect(address)
            return sock
        except OSError as exc:
            failures.append(f"{address[0]} ({exc})")
            sock.close()
    detail = ", ".join(failures[:3]) or "응답한 주소가 없습니다"
    raise OSError(f"{host}:{port} 접속 실패 — {detail}")


def _connect(host: str, port: int, read_timeout: float, ca_bundle: str | None = None) -> ssl.SSLSocket:
    sock = build_context(ca_bundle).wrap_socket(_open_socket(host, port), server_hostname=host)
    sock.settimeout(read_timeout)
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
        ca_bundle: str | None = None,
        connect: Callable[..., ssl.SSLSocket] = _connect,
    ) -> None:
        self.ca_bundle = ca_bundle
        self.username = username
        self.password = password
        self.host = host
        self.port = port
        self.airport = airport
        self.keepalive = keepalive
        self.compression = compression
        self._connect = connect
        self.last_pitr: int | None = None
        # 재접속 백오프 중에도 원인을 UI 에 보여주기 위해 마지막 실패 사유를 남깁니다.
        self.last_connect_error: str | None = None

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
                sock = self._connect(
                    self.host, self.port, self.keepalive + READ_TIMEOUT_MARGIN, self.ca_bundle
                )
                yield from self._session(sock)
                failures = 0
                self.last_connect_error = None
            except (OSError, FirehoseError) as exc:
                self.last_connect_error = describe_failure(exc)
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
