import asyncio
import json
import logging
from urllib.parse import urlparse

import httpx
from aiogram import Bot, Dispatcher

from receipt_bot.config import Settings
from receipt_bot.google_api import GoogleLogin, GoogleStore
from receipt_bot.handlers import LOGINS_PER_MINUTE, PHOTOS_PER_MINUTE, DailyQuota, PendingStore, RateLimiter, router
from receipt_bot.recognition import Recognizer, RecognizerChain
from receipt_bot.storage import Users

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
log = logging.getLogger("receipt_bot")


class RedactingFormatter(logging.Formatter):
    """Замінює секрети на *** у готовому рядку журналу — разом із трейсбеками сторонніх бібліотек.

    Наприклад, aiohttp кладе в текст помилки URL файлу Telegram, а він містить токен бота.
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
    """https://api.groq.com/... -> "groq" — для журналу."""
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
    # httpx на INFO пише кожен URL запиту; ключі в заголовках, але шуму в журналі не треба.
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
        quota=DailyQuota(settings.daily_recognitions),
        users=users,
        login=GoogleLogin(settings.google_oauth_client_file, google_http),
        google=GoogleStore(settings.google_sa_key_file, settings.google_oauth_client_file,
                           settings.google_owner_token_file, settings.sheet_id, settings.drive_folder_id, google_http),
        logins={},
    )
    dp.include_router(router)

    try:
        await dp.start_polling(bot)
    finally:
        await recognizer.close()
        await google_http.aclose()
        users.close()


if __name__ == "__main__":
    asyncio.run(main())
