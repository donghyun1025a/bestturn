from pathlib import Path

import pytest

from incheon_bot.__main__ import build_application
from incheon_bot.bot.handlers import TokenCache
from incheon_bot.settings import Settings


def test_build_application_registers_handlers_and_watch_job(tmp_path):
    settings = Settings(
        telegram_token="123456:TEST-TOKEN",
        service_key="KEY",
        db_path=tmp_path / "b.db",
        config_dir=Path(__file__).resolve().parents[1] / "config",
    )
    app = build_application(settings)
    commands = {
        name
        for handler in app.handlers[0]
        for name in getattr(handler, "commands", []) or []
    }
    assert {"start", "flight", "book", "done", "list", "congestion", "lounge", "status"} <= commands
    assert [job.name for job in app.job_queue.jobs()] == ["watch"]
    assert app.bot_data["ctx"].service.routing.terminals


def test_settings_allowlist():
    open_settings = Settings(telegram_token="t", service_key="k", allowed_user_ids=frozenset())
    assert open_settings.is_allowed(999) is True

    locked = Settings(telegram_token="t", service_key="k", allowed_user_ids=frozenset({7}))
    assert locked.is_allowed(7) is True
    assert locked.is_allowed(8) is False
    assert locked.is_allowed(None) is False


def test_settings_validate_reports_missing():
    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN"):
        Settings(telegram_token="", service_key="k").validate()


def test_token_cache_roundtrip_and_expiry(monkeypatch):
    cache = TokenCache()
    token = cache.put(["flight"], "20260907")
    assert cache.get(token) == (["flight"], "20260907")
    assert cache.get("nope") is None

    import incheon_bot.bot.handlers as handlers

    monkeypatch.setattr(handlers.time, "monotonic", lambda: 10**9)
    assert cache.get(token) is None
