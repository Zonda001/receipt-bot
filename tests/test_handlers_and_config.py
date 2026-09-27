import pytest
from pydantic import ValidationError

from receipt_bot.config import Settings
from receipt_bot.handlers import DailyQuota


def test_daily_quota_give_back():
    q = DailyQuota(2, 5)
    assert q.take("a@x") and q.take("a@x") and not q.take("a@x")
    q.give_back("a@x")
    assert q.take("a@x") and not q.take("a@x")


def test_quota_give_back_never_below_zero():
    q = DailyQuota(1, 5)
    q.give_back("a@x")
    assert q.take("a@x") and not q.take("a@x")


def test_one_person_cannot_use_up_the_team_quota():
    q = DailyQuota(3, 2)
    assert q.take("a@x") and q.take("a@x") and not q.take("a@x")  # a's share is spent, the team still has one
    q.give_back("c@x")  # nothing to give back for someone who took nothing
    assert q.take("b@x") and not q.take("b@x")  # now the team's cap is spent too


def test_settings_errors_do_not_print_values(tmp_path, monkeypatch):
    # Under 50 chars: pydantic truncates longer values anyway; a short one would print in full without the fix.
    # Deliberately not shaped like a Telegram token, so secret scanners don't mistake the test for a real leak.
    fake_token = "not-a-real-secret-" + "q" * 20
    (tmp_path / ".env").write_text(f"BOT_TOKEM={fake_token}\n", encoding="utf-8")  # typo in the key name
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError) as err:
        Settings()
    assert fake_token not in str(err.value)
    assert "bot_tokem" in str(err.value)  # the field name stays, so you can see what to fix


@pytest.mark.parametrize("url, name", [
    ("https://api.cloudflare.com/client/v4/accounts/abc/ai/v1", "cloudflare"),
    ("https://api.groq.com/openai/v1", "groq"),
    ("http://localhost:8000/v1", "localhost"),
])
def test_provider_name_comes_from_the_host(url, name):
    from receipt_bot.__main__ import provider_name
    assert provider_name(url) == name


def test_extra_body_json_survives_dotenv(tmp_path, monkeypatch):
    # Quotes and braces with no wrapping quotes: dotenv must return it as is, or json.loads fails at startup.
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
    assert "reasoning_effort" not in body  # a parameter Cloudflare doesn't know is not sent
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
    # Telegram counts offsets in UTF-16.
    raw = text.encode("utf-16-le")
    return raw[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le")


def test_command_menu_matches_handlers():
    from aiogram.filters import Command

    from receipt_bot.handlers import BOT_COMMANDS, router

    handled = {c for h in router.message.handlers for f in h.filters or []
               if isinstance(f.callback, Command) for c in f.callback.commands if isinstance(c, str)}
    assert {c.command for c in BOT_COMMANDS} <= handled  # the menu doesn't promise commands the bot doesn't know


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
    """/login with fakes: Google hands out a code, and the login never completes."""
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
        await asyncio.sleep(0)  # let the cancellation run
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

    assert run_login(answer)  # the code arrived: the login keeps waiting
    assert len(sent) == 1 and "YNV-JZP-PXGB" in sent[0][0]
    assert sent[0][1] == {"parse_mode": None}  # plain text, as before the buttons


def test_undelivered_login_code_stops_the_login():
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    async def answer(text, **kwargs):  # the person blocked the bot
        raise TelegramForbiddenError(SendMessage(chat_id=1, text=text), "Forbidden: bot was blocked by the user")

    assert not run_login(answer)


# --- "not a receipt" ---

def test_not_a_receipt_keeps_manual_entry_with_the_date_and_currency():
    from datetime import date
    from decimal import Decimal

    from receipt_bot.handlers import not_a_receipt, result_view
    from receipt_bot.recognition import Candidate, Recognition

    rec = not_a_receipt(Recognition(is_receipt=False, candidates=[Candidate("Сума", Decimal("5.00"))], has_total=True,
                                    currency="PLN", receipt_date=date(2026, 9, 24)))
    assert rec.is_receipt and rec.candidates == [] and not rec.has_total
    assert rec.currency == "PLN" and rec.receipt_date == date(2026, 9, 24)  # a typed amount keeps what was read
    text, kb = result_view("r1", rec, "Не схоже на чек чи квитанцію про оплату.")
    buttons = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert text.startswith("Не схоже на чек") and len(buttons) == 2  # only "enter manually" and "cancel"
    assert all(":ok:" not in data for data in buttons)


# --- Google down while saving (live test 28.09) ---

def test_save_stops_before_the_upload_when_google_is_down():
    import asyncio
    from decimal import Decimal

    from receipt_bot.google_api import GoogleUnreachable, ServiceDown
    from receipt_bot.handlers import NotSaved, Pending, SaveLimit, not_saved_text, save
    from receipt_bot.recognition import Recognition

    calls = []

    class Google:
        async def has_access(self, email):
            return True

        async def ping(self):
            raise ServiceDown("Sheets", GoogleUnreachable("sheets check: ConnectError"))

        async def save_receipt(self, *args):
            calls.append("save")

    class Bot:
        async def download(self, file_id):
            calls.append("download")

    class Users:
        def email(self, user_id):
            return "a@x"

    saves = SaveLimit(1, 10**9)
    item = Pending(user_id=1, file_id="f", recognition=Recognition(is_receipt=True), created=0.0, chat_id=1, msg_id=1)
    with pytest.raises(NotSaved) as err:
        asyncio.run(save(Bot(), "rid", item, Decimal("1.00"), False, Users(), Google(), saves))
    assert calls == []  # no photo downloaded, nothing uploaded
    assert not_saved_text(err.value) == (
        "⚠️ Не вдається з'єднатися з Google Таблицями. Чек не записано — можна натиснути ще раз.")
    assert saves.take("a@x", 1)  # the day's only save wasn't spent on a try that couldn't work


def test_unsure_save_does_not_also_say_not_saved():
    # live 28.09: "not sure it saved, check the sheet" + "not saved, press again" in one message
    from receipt_bot.handlers import NotSaved, not_saved_text

    unsure = not_saved_text(NotSaved("Google не відповів вчасно — не впевнений, чи чек записався.", unsure=True))
    assert "не записано" not in unsure and unsure.startswith("⚠️ Google не відповів")
    assert not_saved_text(NotSaved("Не вдалося записати в Google.")).endswith("Чек не записано — можна натиснути ще раз.")
