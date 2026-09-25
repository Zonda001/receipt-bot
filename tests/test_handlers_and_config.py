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


@pytest.mark.parametrize("url, name", [
    ("https://api.cloudflare.com/client/v4/accounts/abc/ai/v1", "cloudflare"),
    ("https://api.groq.com/openai/v1", "groq"),
    ("http://localhost:8000/v1", "localhost"),
])
def test_provider_name_comes_from_the_host(url, name):
    from receipt_bot.__main__ import provider_name
    assert provider_name(url) == name


def test_extra_body_json_survives_dotenv(tmp_path, monkeypatch):
    # Значення з лапками й дужками без обгортки в лапки: dotenv має віддати його як є, інакше json.loads впаде на старті.
    (tmp_path / ".env").write_text(
        "BOT_TOKEN=t\nGOOGLE_SA_KEY_FILE=a\nGOOGLE_OAUTH_CLIENT_FILE=b\nSHEET_ID=c\nGOOGLE_OWNER_TOKEN_FILE=d\n"
        "DRIVE_FOLDER_ID=e\nLLM_BASE_URL=https://api.cloudflare.com/x/ai/v1\nLLM_MODEL=m\nLLM_API_KEY=k\n"
        'LLM_EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": false}}\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    from receipt_bot.__main__ import make_recognizer
    s = Settings()
    rec = make_recognizer(s.llm_base_url, s.llm_model, "k", s.llm_reasoning_effort, s.llm_extra_body)
    body = rec._request_body(b"jpeg")
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in body  # чужий для Cloudflare параметр не надсилається
    assert rec.name == "cloudflare"
