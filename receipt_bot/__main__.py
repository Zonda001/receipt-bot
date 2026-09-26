import asyncio
import json
import logging
from urllib.parse import urlparse

import httpx
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommandScopeAllPrivateChats

from receipt_bot.config import Settings
from receipt_bot.google_api import GoogleLogin, GoogleStore
from receipt_bot.handlers import (
    BOT_COMMANDS, LOGINS_GLOBAL_PER_MINUTE, LOGINS_PER_MINUTE, PHOTOS_PER_MINUTE, REPLIES_PER_MINUTE, SAVES_PER_DAY,
    DailyQuota, PendingStore, RateLimiter, SaveLimit, router,
)
from receipt_bot.recognition import Recognizer, RecognizerChain
from receipt_bot.storage import Users

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
log = logging.getLogger("receipt_bot")


class RedactingFormatter(logging.Formatter):
    """Replaces secrets with *** in the finished log line, tracebacks from third-party libraries included.

    For example, aiohttp puts the Telegram file URL, which contains the bot token, into its error text.
    """

    def __init__(self, secrets: list[str]) -> None:
        super().__init__(LOG_FORMAT)
        self._secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


PROTECTED_FIELDS = {"model", "messages", "response_format"}


def provider_name(base_url: str) -> str:
    """https://api.groq.com/... -> "groq", for the journal."""
    host = urlparse(base_url).hostname or "llm"
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else host


def parse_extra_body(raw: str) -> dict | None:
    if not raw.strip():
        return None
    extra = json.loads(raw)
    if not isinstance(extra, dict):
        raise SystemExit("LLM_*EXTRA_BODY має бути JSON-об'єктом {...}")
    if clash := PROTECTED_FIELDS & extra.keys():
        raise SystemExit(f"LLM_*EXTRA_BODY не може міняти {sorted(clash)}")
    return extra


def make_recognizer(base_url: str, model: str, api_key: str, reasoning_effort: str, extra_body: str) -> Recognizer:
    return Recognizer(base_url=base_url, model=model, api_key=api_key, reasoning_effort=reasoning_effort,
                      extra_body=parse_extra_body(extra_body), name=provider_name(base_url))


async def main() -> None:
    settings = Settings()
    bot_token = settings.bot_token.get_secret_value()
    llm_key = settings.llm_api_key.get_secret_value()
    fallback_key = settings.llm_fallback_api_key.get_secret_value()

    logging.basicConfig(level=logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter([bot_token, llm_key, fallback_key]))
    # At INFO httpx logs every request URL; keys are in headers, but the journal doesn't need the noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    primary = make_recognizer(settings.llm_base_url, settings.llm_model, llm_key,
                              settings.llm_reasoning_effort, settings.llm_extra_body)
    fallback = None
    if settings.llm_fallback_base_url:
        fallback = make_recognizer(settings.llm_fallback_base_url, settings.llm_fallback_model, fallback_key,
                                   settings.llm_fallback_reasoning_effort, settings.llm_fallback_extra_body)
    log.info("vision providers: %s", " -> ".join(p.name for p in (primary, fallback) if p))
    recognizer = RecognizerChain(primary, fallback)

    google_http = httpx.AsyncClient(timeout=30)
    users = Users(settings.db_path)
    bot = Bot(bot_token)
    dp = Dispatcher(
        recognizer=recognizer,
        pending=PendingStore(),
        limiter=RateLimiter(PHOTOS_PER_MINUTE),
        login_limiter=RateLimiter(LOGINS_PER_MINUTE),
        login_global=RateLimiter(LOGINS_GLOBAL_PER_MINUTE),
        chatter=RateLimiter(REPLIES_PER_MINUTE),
        saves=SaveLimit(SAVES_PER_DAY),
        quota=DailyQuota(settings.daily_recognitions),
        users=users,
        login=GoogleLogin(settings.google_oauth_client_file, google_http),
        google=GoogleStore(settings.google_sa_key_file, settings.google_oauth_client_file,
                           settings.google_owner_token_file, settings.sheet_id, settings.drive_folder_id, google_http),
        logins={},
    )
    dp.include_router(router)

    try:
        try:  # the "/" menu in the client; the bot works without it too
            await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats(), request_timeout=10)
        except TelegramAPIError as e:
            log.warning("can't set the command menu: %s", type(e).__name__)
        await dp.start_polling(bot, tasks_concurrency_limit=50)  # cap on concurrent updates
    finally:
        await recognizer.close()
        await google_http.aclose()
        await bot.session.close()  # polling closes it itself, but we may never have got that far
        users.close()


if __name__ == "__main__":
    asyncio.run(main())
