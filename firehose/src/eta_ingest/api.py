"""적재된 도착 예정시각 조회 API (표준 라이브러리 HTTP 서버)."""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .protocol import airport_icao
from .settings import Settings
from .storage import EtaStore

MAX_WINDOW_HOURS = 72


def _int_param(params: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int(params.get(name, [""])[0])
    except ValueError:
        return default


def build_handler(store: EtaStore, settings: Settings) -> type[BaseHTTPRequestHandler]:
    default_dest = airport_icao(settings.airport)

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

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 규약
            url = urlparse(self.path)
            params = parse_qs(url.query)

            if url.path == "/health":
                return self._send(200, {"ok": True, "pitr": store.get_pitr()})

            if url.path == "/arrivals":
                hours = max(1, min(_int_param(params, "hours", 12), MAX_WINDOW_HOURS))
                now = int(time.time())
                dest = (params.get("dest", [default_dest])[0]).strip().upper()
                rows = store.arrivals(airport_icao(dest), since=now - 3600, until=now + hours * 3600)
                return self._send(200, {"dest": airport_icao(dest), "hours": hours, "flights": rows})

            if url.path == "/flight":
                ident = (params.get("ident", [""])[0]).strip().upper()
                if not ident:
                    return self._send(400, {"error": "ident 파라미터가 필요합니다"})
                rows = store.by_ident(ident)
                if not rows:
                    return self._send(404, {"error": f"{ident} 기록이 없습니다"})
                return self._send(200, {"ident": ident, "flights": rows})

            if url.path == "/history":
                flight_id = (params.get("flight_id", [""])[0]).strip()
                if not flight_id:
                    return self._send(400, {"error": "flight_id 파라미터가 필요합니다"})
                flight = store.flight(flight_id)
                if flight is None:
                    return self._send(404, {"error": f"{flight_id} 기록이 없습니다"})
                return self._send(200, {"flight": flight, "eta_history": store.history(flight_id)})

            self._send(404, {"error": "알 수 없는 경로"})

    return Handler


def serve(settings: Settings) -> None:
    store = EtaStore(settings.db_path)
    server = ThreadingHTTPServer((settings.api_host, settings.api_port), build_handler(store, settings))
    server.serve_forever()
