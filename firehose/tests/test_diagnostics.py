"""사내 방화벽의 TLS 검사와 윈도우 루트 저장소 문제를 가려내는 진단."""
from __future__ import annotations

import socket
import ssl
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from eta_ingest import diagnostics
from eta_ingest.client import build_context, describe_failure


@pytest.fixture(scope="module")
def self_signed(tmp_path_factory) -> tuple[Path, Path]:
    """사내 프록시가 제시하는 것과 같은, 신뢰되지 않는 인증서."""
    directory = tmp_path_factory.mktemp("certs")
    cert, key = directory / "c.pem", directory / "k.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(cert),
         "-days", "1", "-nodes", "-subj", "/CN=localhost/O=Fake Proxy"],
        check=True, capture_output=True,
    )
    return cert, key


@pytest.fixture
def tls_server(self_signed):
    cert, key = self_signed
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                client, _ = listener.accept()
            except OSError:
                return
            try:
                context.wrap_socket(client, server_side=True)
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1], cert
    stop.set()
    listener.close()


def test_untrusted_certificate_is_reported_as_a_failure(tls_server, settings, capsys):
    port, _ = tls_server

    exit_code = diagnostics.run(replace(settings, host="localhost", port=port, ca_bundle=""))

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "사내 방화벽" in out
    assert "FIREHOSE_CA_BUNDLE" in out
    assert "검증" in out and "끄는 방법은 안내하지 않습니다" in out


def test_supplying_the_corporate_ca_resolves_it(tls_server, settings, capsys):
    port, cert = tls_server

    exit_code = diagnostics.run(replace(settings, host="localhost", port=port, ca_bundle=str(cert)))

    assert exit_code == 0
    assert "접속 경로에 문제가 없습니다" in capsys.readouterr().out


def test_blocked_port_is_distinguished_from_a_certificate_problem(settings, capsys):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()

    exit_code = diagnostics.run(replace(settings, host="localhost", port=port))

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "방화벽에 막혀" in out
    assert "206.253.80.0/21" in out
    assert "사내 방화벽" not in out  # 인증서 안내가 섞이면 안 됩니다


def test_certificate_error_tells_the_user_what_to_run():
    message = describe_failure(ssl.SSLCertVerificationError("certificate verify failed"))
    assert "doctor" in message


def test_supplied_ca_bundle_is_the_only_trust_anchor(self_signed):
    cert, _ = self_signed
    context = build_context(str(cert))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
