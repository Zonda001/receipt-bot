import pytest
from pydantic import ValidationError

from receipt_bot.config import Settings
from receipt_bot.handlers import DailyQuota


def test_daily_quota_give_back():
    q = DailyQuota(2)
    assert q.take() and q.take() and not q.take()
    q.give_back()
    assert q.take() and not q.take()


def test_quota_give_back_never_below_zero():
    q = DailyQuota(1)
    q.give_back()
    assert q.take() and not q.take()


def test_settings_errors_do_not_print_values(tmp_path, monkeypatch):
    # Коротше 50 символів: довші значення pydantic і так обрізає, а коротке без виправлення друкується повністю.
    # Навмисно не у форматі токена Telegram, щоб сканери секретів не плутали тест зі справжнім витоком.
    fake_token = "not-a-real-secret-" + "q" * 20
    (tmp_path / ".env").write_text(f"BOT_TOKEM={fake_token}\n", encoding="utf-8")  # опечатка в назві ключа
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError) as err:
        Settings()
    assert fake_token not in str(err.value)
    assert "bot_tokem" in str(err.value)  # назва поля лишається — видно, що виправити
