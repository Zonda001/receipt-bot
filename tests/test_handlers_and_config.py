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


# --- login UX ---

def test_login_prompt_code_is_copyable_and_google_opens():
    import receipt_bot.google_api as g
    from receipt_bot.handlers import login_prompt

    msg = login_prompt(g.DeviceCode("dc", "YNV-JZP-PXGB", "https://www.google.com/device", 1800, 5))
    buttons = [b for row in msg["reply_markup"].inline_keyboard for b in row]
    assert any(b.copy_text and b.copy_text.text == "YNV-JZP-PXGB" for b in buttons)
    assert any(b.url == "https://www.google.com/device" for b in buttons)
    code = [e for e in msg["entities"] if e.type == "code"]
    assert len(code) == 1 and e_text(msg["text"], code[0]) == "YNV-JZP-PXGB"
    assert "30 хв" in msg["text"]


def e_text(text: str, entity) -> str:
    # Telegram рахує зсуви в UTF-16.
    raw = text.encode("utf-16-le")
    return raw[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le")


def test_command_menu_matches_handlers():
    from aiogram.filters import Command

    from receipt_bot.handlers import BOT_COMMANDS, router

    handled = {c for h in router.message.handlers for f in h.filters or []
               if isinstance(f.callback, Command) for c in f.callback.commands if isinstance(c, str)}
    assert {c.command for c in BOT_COMMANDS} <= handled  # меню не обіцяє команд, яких бот не знає


def test_login_prompt_skips_a_non_https_link_but_keeps_copy():
    import receipt_bot.google_api as g
    from receipt_bot.handlers import login_prompt

    msg = login_prompt(g.DeviceCode("dc", "ABC", "www.google.com/device", 1800, 5))
    buttons = [b for row in msg["reply_markup"].inline_keyboard for b in row]
    assert [b.copy_text.text for b in buttons] == ["ABC"]


def test_login_without_access_leaves_a_trace_but_no_email(caplog):
    import asyncio
    import logging
    from types import SimpleNamespace

    import receipt_bot.google_api as g
    from receipt_bot.handlers import finish_login

    answers = []

    async def answer(text, **kwargs):
        answers.append(text)

    async def wait_for_email(code):
        return "stranger@gmail.com"

    async def has_access(email):
        return False

    def link(user_id, email):
        raise AssertionError("email without access must not be stored")

    message = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=answer)
    code = g.DeviceCode("dc", "X", "https://www.google.com/device", 1800, 5)
    with caplog.at_level(logging.INFO, logger="receipt_bot.handlers"):
        asyncio.run(finish_login(message, code, SimpleNamespace(link=link), SimpleNamespace(has_access=has_access),
                                 SimpleNamespace(wait_for_email=wait_for_email), {}))
    assert "user 42 signed in without sheet access" in caplog.text
    assert "stranger" not in caplog.text
    assert "не має доступу" in answers[0]


def run_login(answer):
    """/login з фейками: Google видає код, а вхід ніколи не завершується."""
    import asyncio
    from types import SimpleNamespace

    import receipt_bot.google_api as g
    from receipt_bot.handlers import RateLimiter, on_login

    async def start():
        return g.DeviceCode("dc", "YNV-JZP-PXGB", "https://www.google.com/device", 1800, 5)

    async def never_finishes(code):
        await asyncio.sleep(3600)

    async def scenario():
        logins = {}
        message = SimpleNamespace(from_user=SimpleNamespace(id=7), answer=answer)
        await on_login(message, SimpleNamespace(), SimpleNamespace(),
                       SimpleNamespace(start=start, wait_for_email=never_finishes),
                       RateLimiter(3), RateLimiter(20), logins)
        task = logins.pop(7, None)
        await asyncio.sleep(0)  # дати скасуванню відпрацювати
        alive = task is not None and not task.done()
        if task:
            task.cancel()
        return alive

    return asyncio.run(scenario())


def bad_request(text: str):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    return TelegramBadRequest(SendMessage(chat_id=1, text=text), "Bad Request: BUTTON_URL_INVALID")


def test_login_code_still_arrives_as_plain_text_if_rejected():
    sent = []

    async def answer(text, **kwargs):
        if kwargs.get("reply_markup") or kwargs.get("entities"):
            raise bad_request(text)
        sent.append((text, kwargs))

    assert run_login(answer)  # код дійшов — вхід чекає далі
    assert len(sent) == 1 and "YNV-JZP-PXGB" in sent[0][0]
    assert sent[0][1] == {"parse_mode": None}  # голий текст, як до кнопок


def test_undelivered_login_code_stops_the_login():
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    async def answer(text, **kwargs):  # людина заблокувала бота
        raise TelegramForbiddenError(SendMessage(chat_id=1, text=text), "Forbidden: bot was blocked by the user")

    assert not run_login(answer)
