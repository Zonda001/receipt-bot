import asyncio
import json
import logging
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher

from receipt_bot.config import Settings
from receipt_bot.handlers import PHOTOS_PER_MINUTE, DailyQuota, PendingStore, RateLimiter, router
from receipt_bot.recognition import Recognizer, RecognizerChain

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


def provider_name(base_url: str) -> str:
    """https://api.cloudflare.com/... -> "cloudflare", https://api.groq.com/... -> "groq": щоб у журналі було видно, хто відповів."""
    host = urlparse(base_url).hostname or "llm"
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else host


def make_recognizer(base_url: str, model: str, api_key: str, reasoning_effort: str, extra_body: str) -> Recognizer:
    return Recognizer(base_url=base_url, model=model, api_key=api_key, reasoning_effort=reasoning_effort,
                      extra_body=json.loads(extra_body) if extra_body.strip() else None,
                      name=provider_name(base_url))


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
    bot = Bot(bot_token)
    dp = Dispatcher(
        recognizer=recognizer,
        pending=PendingStore(),
        limiter=RateLimiter(PHOTOS_PER_MINUTE),
        quota=DailyQuota(settings.daily_recognitions),
        allowed_ids=settings.allowed_ids,
    )
    dp.include_router(router)

    try:
        await dp.start_polling(bot)
    finally:
        await recognizer.close()


if __name__ == "__main__":
    asyncio.run(main())
