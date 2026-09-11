"""소켓을 가짜로 바꿔 네트워크 없이 클라이언트 동작을 고정합니다."""
from __future__ import annotations

import json
import zlib

import pytest
from eta_ingest.client import STALE_KEEPALIVE_LIMIT, FirehoseClient, FirehoseError


class FakeSocket:
    """보낸 명령을 기록하고, 준비된 바이트를 조각내어 돌려줍니다."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def close(self) -> None:
        self.closed = True


def lines(*messages: dict) -> bytes:
    return b"".join(json.dumps(m).encode() + b"\n" for m in messages)


def client_for(sock: FakeSocket, **kwargs) -> FirehoseClient:
    return FirehoseClient(
        username="u", password="k", airport="ICN",
        connect=lambda *_: sock, **kwargs,
    )


def test_session_sends_init_command_then_yields_messages():
    sock = FakeSocket([lines({"type": "position", "id": "A-1", "ident": "THA657", "pitr": "100"})])
    client = client_for(sock, compression=None)

    received = list(client._session(sock))

    assert sock.sent[0].decode().startswith("live username u password k")
    assert [m["type"] for m in received] == ["position"]
    assert client.last_pitr == 100


def test_messages_split_across_tcp_chunks_are_reassembled():
    payload = lines(
        {"type": "flifo", "id": "A-1", "ident": "THA657", "pitr": "100"},
        {"type": "flifo", "id": "A-2", "ident": "AAL281", "pitr": "101"},
    )
    sock = FakeSocket([payload[:20], payload[20:45], payload[45:]])
    client = client_for(sock, compression=None)

    assert len(list(client._session(sock))) == 2


def test_gzip_stream_is_decompressed_but_init_command_is_not():
    compressor = zlib.compressobj(wbits=16 | zlib.MAX_WBITS)
    body = compressor.compress(lines({"type": "flifo", "id": "A-1", "ident": "THA657", "pitr": "7"}))
    sock = FakeSocket([body + compressor.flush()])
    client = client_for(sock, compression="gzip")

    received = list(client._session(sock))

    assert b"compression gzip" in sock.sent[0]
    assert sock.sent[0].startswith(b"live ")  # 초기화 명령 자체는 평문
    assert received[0]["id"] == "A-1"


def test_reconnect_resumes_from_last_pitr():
    sock = FakeSocket([lines({"type": "flifo", "id": "A-1", "ident": "THA657", "pitr": "555"})])
    client = client_for(sock, compression=None)
    list(client._session(sock))

    list(client._session(FakeSocket([])))

    assert sock.sent[0].startswith(b"live ")
    assert client.last_pitr == 555


def test_server_error_message_raises():
    sock = FakeSocket([lines({"type": "error", "pitr": "1", "error_msg": "bad password"})])
    client = client_for(sock, compression=None)

    with pytest.raises(FirehoseError, match="bad password"):
        list(client._session(sock))


def test_stalled_keepalive_pitr_ends_the_session():
    keepalive = {"type": "keepalive", "serverTime": "1", "pitr": "900"}
    sock = FakeSocket([lines(*[keepalive] * (STALE_KEEPALIVE_LIMIT + 3))])
    client = client_for(sock, compression=None)

    received = list(client._session(sock))

    assert len(received) < STALE_KEEPALIVE_LIMIT + 3
