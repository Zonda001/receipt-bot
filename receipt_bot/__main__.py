import asyncio
import logging

from aiogram import Bot, Dispatcher

from receipt_bot.config import Settings
from receipt_bot.handlers import PHOTOS_PER_MINUTE, DailyQuota, PendingStore, RateLimiter, router
from receipt_bot.recognition import Recognizer

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


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


async def main() -> None:
    settings = Settings()
    bot_token = settings.bot_token.get_secret_value()
    llm_key = settings.llm_api_key.get_secret_value()

    logging.basicConfig(level=logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter([bot_token, llm_key]))
    # httpx на INFO пише кожен URL запиту; ключі в заголовках, але шуму в журналі не треба.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    recognizer = Recognizer(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=llm_key,
        reasoning_effort=settings.llm_reasoning_effort,
    )
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
