"""접속이 안 될 때 원인을 좁혀 주는 진단 (`python run.py doctor`).

DNS → TCP → TLS 순으로 확인하고, TLS 가 막히면 신뢰 설정을 바꿔 가며
"윈도우 루트 저장소 문제"인지 "사내 방화벽의 TLS 검사"인지 가려냅니다.
"""
from __future__ import annotations

import hashlib
import socket
import ssl

from .client import CONNECT_TIMEOUT, build_context, _open_socket
from .settings import Settings

OK = "[ 정상 ]"
FAIL = "[ 실패 ]"


def _handshake(host: str, port: int, context: ssl.SSLContext) -> tuple[bool, str]:
    try:
        with context.wrap_socket(_open_socket(host, port), server_hostname=host) as sock:
            return True, sock.version() or "TLS"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _peer_fingerprint(host: str, port: int) -> str | None:
    """검증 없이 인증서 지문만 확인합니다 (진단 전용, 데이터는 주고받지 않습니다)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with context.wrap_socket(_open_socket(host, port), server_hostname=host) as sock:
            der = sock.getpeercert(binary_form=True)
    except Exception:
        return None
    return hashlib.sha256(der).hexdigest()[:32] if der else None


def run(settings: Settings) -> int:
    host, port = settings.host, settings.port
    print(f"\n대상: {host}:{port}\n" + "-" * 60)

    try:
        addresses = [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
        print(f"{OK} DNS — 주소 {len(addresses)}개 ({', '.join(addresses[:3])} …)")
    except OSError as exc:
        print(f"{FAIL} DNS — {exc}")
        print("\n→ 인터넷 연결 또는 DNS 설정을 확인하세요.")
        return 1

    try:
        _open_socket(host, port).close()
        print(f"{OK} TCP {port} 포트 — 연결됨")
    except OSError as exc:
        print(f"{FAIL} TCP {port} 포트 — {exc}")
        print(
            f"\n→ {port} 포트가 방화벽에 막혀 있습니다 (웹과 달리 443 이 아닙니다)."
            f"\n  전산팀에 'firehose.flightaware.com 의 {port}/TCP 아웃바운드 허용'을 요청하세요."
            f"\n  허용이 필요한 대역: 206.253.80.0/21"
        )
        return 1

    no_verify = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    no_verify.check_hostname = False
    no_verify.verify_mode = ssl.CERT_NONE

    probes = [("trusted", "기본 설정", build_context())]
    if settings.ca_bundle:
        probes.append(("trusted", f"지정한 CA ({settings.ca_bundle})", build_context(settings.ca_bundle)))
    probes.append(("unverified", "검증 없음(진단용)", no_verify))

    verified = False
    tls_reachable = False
    for kind, label, context in probes:
        ok, detail = _handshake(host, port, context)
        verified = verified or (ok and kind == "trusted")
        tls_reachable = tls_reachable or ok
        print(f"{OK if ok else FAIL} TLS · {label} — {detail}")

    if verified:
        print("\n→ 접속 경로에 문제가 없습니다. 실패가 계속되면 사용자명/API Key 를 확인하세요.")
        return 0

    fingerprint = _peer_fingerprint(host, port)
    if fingerprint:
        print(f"\n받은 인증서 지문(SHA-256 앞 16바이트): {fingerprint}")

    if tls_reachable:
        print(
            "\n→ TLS 자체는 되지만 인증서를 신뢰할 수 없습니다."
            "\n  사내 방화벽이 통신을 가로채고 자체 인증서를 제시하는 환경으로 보입니다."
            "\n  전산팀에서 '사내 루트 CA 인증서'(.cer/.pem)를 받아 파일로 저장한 뒤,"
            "\n  같은 폴더의 .env 파일에 아래 한 줄을 넣고 다시 실행하세요."
            "\n"
            "\n      FIREHOSE_CA_BUNDLE=C:\\경로\\사내CA.pem"
            "\n"
            "\n  (인증서 검증을 끄는 방법은 안내하지 않습니다. 자격증명이 그대로 노출됩니다.)"
        )
    else:
        print("\n→ TLS 핸드셰이크 자체가 실패했습니다. 방화벽이 이 포트의 통신을 끊고 있을 수 있습니다.")
    return 1
