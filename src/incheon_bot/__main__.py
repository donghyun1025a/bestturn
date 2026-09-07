"""봇 실행 엔트리포인트: python -m incheon_bot"""
from __future__ import annotations

import logging

from telegram.ext import Application, Defaults
from telegram.constants import ParseMode

from .api.client import IncheonAirportClient
from .bot import handlers
from .service import BriefingService
from .settings import Settings
from .storage.db import BookingStore
from .watcher import WATCH_TICK_SECONDS, watch_tick


def build_application(settings: Settings) -> Application:
    client = IncheonAirportClient(
        settings.service_key,
        flight_daily_budget=settings.flight_daily_budget,
        congestion_daily_budget=settings.congestion_daily_budget,
    )
    service = BriefingService(client, settings.config_dir)
    store = BookingStore(settings.db_path)

    app = (
        Application.builder()
        .token(settings.telegram_token)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .post_shutdown(lambda _app: client.aclose())
        .build()
    )
    app.bot_data["ctx"] = handlers.BotContext(settings, service, store)
    handlers.register(app)
    if app.job_queue is None:
        raise SystemExit(
            "JobQueue 를 사용할 수 없습니다. `pip install \"python-telegram-bot[job-queue]\"` 로 설치하세요."
        )
    app.job_queue.run_repeating(watch_tick, interval=WATCH_TICK_SECONDS, first=10, name="watch")
    return app


def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings.validate()
    app = build_application(settings)
    logging.getLogger(__name__).info("인천공항 의전 봇을 시작합니다.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
