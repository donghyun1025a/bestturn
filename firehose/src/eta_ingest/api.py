"""웹 UI + 조회/제어 API (표준 라이브러리 HTTP 서버)."""
from __future__ import annotations

import json
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import compare as compare_module
from . import config as config_module
from .iia import KST, IiaClient, IiaError
from .protocol import airport_icao
from .runner import IngestRunner
from .settings import Settings, checkout_version
from .storage import EtaStore

WEB_ROOT = Path(__file__).parent / "web"
MAX_WINDOW_HOURS = 72
MAX_BODY_BYTES = 64 * 1024


def _int_param(params: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int(params.get(name, [""])[0])
    except ValueError:
        return default


def _str_param(params: dict[str, list[str]], name: str, default: str = "") -> str:
    return (params.get(name, [default])[0] or "").strip()


class Api:
    """HTTP 핸들러와 분리해 둔 실제 동작. 테스트에서 직접 부릅니다."""

    def __init__(self, store: EtaStore, base: Settings, runner: IngestRunner | None = None) -> None:
        self.store = store
        self.base = base
        self.runner = runner or IngestRunner(store)
        self._iia: tuple[str, IiaClient] | None = None

    def settings(self) -> Settings:
        return config_module.effective(self.base, self.store)

    def _iia_client(self, key: str) -> IiaClient:
        # 서비스키가 바뀌면 캐시째로 새로 만듭니다.
        if self._iia is None or self._iia[0] != key:
            self._iia = (key, IiaClient(key))
        return self._iia[1]

    def get(self, path: str, params: dict[str, list[str]]) -> tuple[int, dict]:
        settings = self.settings()

        if path == "/api/config":
            return 200, config_module.public_view(settings)

        if path == "/api/status":
            return 200, {
                **self.runner.status(),
                "configured": bool(settings.username and settings.password),
                "airlines": list(settings.airlines),
                "airport": settings.airport,
                "server_time": time.time(),
                "version": checkout_version(),
                "db_path": str(settings.db_path),
            }

        if path == "/api/arrivals":
            hours = max(1, min(_int_param(params, "hours", 12), MAX_WINDOW_HOURS))
            now = int(time.time())
            dest = airport_icao(_str_param(params, "dest", settings.airport) or settings.airport)
            rows = self.store.arrivals(dest, since=now - 3600, until=now + hours * 3600)
            # 범위 밖이라 비어 있는 것인지, 아직 아무것도 못 받은 것인지 화면이 구분해야 합니다.
            return 200, {"dest": dest, "hours": hours, "flights": rows, "stored": self.store.span()}

        if path == "/api/flight":
            ident = _str_param(params, "ident").upper()
            if not ident:
                return 400, {"error": "편명(ident)을 입력해 주세요."}
            rows = self.store.by_ident(ident)
            if not rows:
                return 404, {"error": f"{ident} 기록이 없습니다."}
            return 200, {"ident": ident, "flights": rows}

        if path == "/api/history":
            flight_id = _str_param(params, "flight_id")
            if not flight_id:
                return 400, {"error": "flight_id 가 필요합니다."}
            flight = self.store.flight(flight_id)
            if flight is None:
                return 404, {"error": f"{flight_id} 기록이 없습니다."}
            return 200, {"flight": flight, "eta_history": self.store.history(flight_id)}

        if path == "/api/compare":
            return self._compare(params, settings)

        return 404, {"error": "알 수 없는 경로입니다."}

    def _compare(self, params: dict[str, list[str]], settings: Settings) -> tuple[int, dict]:
        if not settings.service_key:
            return 400, {"error": "공공데이터 서비스키가 설정되지 않아 대조할 수 없습니다."}
        hours = max(1, min(_int_param(params, "hours", 12), MAX_WINDOW_HOURS))
        now = int(time.time())
        rows = self.store.arrivals(airport_icao(settings.airport), since=now - 3600, until=now + hours * 3600)
        search_date = _str_param(params, "date") or datetime.now(KST).strftime("%Y%m%d")
        try:
            arrivals = self._iia_client(settings.service_key).arrivals(search_date)
        except (IiaError, OSError) as exc:
            return 502, {"error": f"공공데이터 조회 실패: {exc}"}
        results = compare_module.compare(rows, arrivals)
        matched = sum(1 for r in results if r["matched_by"])
        return 200, {
            "date": search_date,
            "hours": hours,
            "matched": matched,
            "total": len(results),
            "rows": results,
        }

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        if path == "/api/config":
            config_module.save(self.store, payload)
            return 200, {"ok": True, "config": config_module.public_view(self.settings())}

        if path == "/api/ingest/start":
            settings = self.settings()
            if not (settings.username and settings.password):
                return 400, {"error": "Firehose 사용자명과 API Key 를 먼저 저장해 주세요."}
            started = self.runner.start(settings)
            return 200, {"ok": True, "started": started, "status": self.runner.status()}

        if path == "/api/ingest/stop":
            stopped = self.runner.stop()
            return 200, {"ok": True, "stopped": stopped, "status": self.runner.status()}

        return 404, {"error": "알 수 없는 경로입니다."}


def build_handler(api: Api, *, allowed_hosts: frozenset[str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "eta-ingest"

        def log_message(self, fmt: str, *args: object) -> None:  # 액세스 로그는 끕니다
            pass

        def _send(self, status: int, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _guard(self) -> bool:
            """DNS 리바인딩과 다른 사이트에서의 교차 출처 요청을 막습니다.

            자격증명을 다루는 로컬 서버라 Host/Origin 을 직접 확인합니다.
            """
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host and host not in allowed_hosts:
                self._send(403, {"error": f"허용되지 않은 Host 입니다: {host}"})
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in allowed_hosts:
                self._send(403, {"error": "허용되지 않은 Origin 입니다."})
                return False
            return True

        def _serve_ui(self) -> None:
            page = WEB_ROOT / "index.html"
            body = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 규약
            if not self._guard():
                return
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._serve_ui()
            status, payload = api.get(url.path, parse_qs(url.query))
            self._send(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            # 브라우저가 폼으로 교차 출처 POST 를 못 보내도록 JSON 만 받습니다.
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._send(415, {"error": "Content-Type 은 application/json 이어야 합니다."})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "Content-Length 가 올바르지 않습니다."})
            if length > MAX_BODY_BYTES:
                return self._send(413, {"error": "요청이 너무 큽니다."})
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "JSON 을 해석할 수 없습니다."})
            if not isinstance(payload, dict):
                return self._send(400, {"error": "JSON 객체를 보내주세요."})
            status, response = api.post(urlparse(self.path).path, payload)
            self._send(status, response)

    return Handler


def serve(settings: Settings) -> None:
    store = EtaStore(settings.db_path)
    api = Api(store, settings)
    allowed = frozenset({settings.api_host, "localhost", "127.0.0.1", "[::1]", "::1"})
    server = ThreadingHTTPServer((settings.api_host, settings.api_port), build_handler(api, allowed_hosts=allowed))
    server.serve_forever()
