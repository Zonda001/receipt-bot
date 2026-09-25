from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Усі налаштування з .env. SecretStr не друкує значення в логах і repr."""

    # hide_input_in_errors: при помилці в .env pydantic інакше друкує значення (токени!) у журнал.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", hide_input_in_errors=True)

    bot_token: SecretStr

    google_sa_key_file: str
    google_oauth_client_file: str
    sheet_id: str

    google_owner_token_file: str
    drive_folder_id: str

    # Основний провайдер vision-моделі (будь-який OpenAI-сумісний API). Готові блоки для Cloudflare і Groq —
    # у .env.example. "Вимкнути міркування" кожен провайдер задає по-своєму, тому обидва поля нижче
    # порожні за замовчуванням: чужий параметр провайдер може відхилити з помилкою 400.
    llm_base_url: str
    llm_model: str
    llm_api_key: SecretStr
    llm_reasoning_effort: str = ""   # Groq/OpenAI: "none"
    llm_extra_body: str = ""         # JSON, що додається до запиту. Cloudflare Gemma: {"chat_template_kwargs": {"enable_thinking": false}}

    # Запасний провайдер (порожній base_url — вимкнено): бере фото, коли основний вичерпав денний ліміт,
    # впав або відповів сміттям.
    llm_fallback_base_url: str = ""
    llm_fallback_model: str = ""
    llm_fallback_api_key: SecretStr = SecretStr("")
    llm_fallback_reasoning_effort: str = ""
    llm_fallback_extra_body: str = ""

    # Стеля розпізнавань на добу на всю команду — запобіжник від зациклення чи спаму.
    # Реальні стелі безкоштовних тарифів (заміряно 25.09): Cloudflare — 10K нейронів/добу, ~9 на чек -> ~1100 чеків;
    # Groq — 200K токенів/добу, ~2.3K на чек -> ~80 чеків. Лише з Groq варто поставити ~80.
    daily_recognitions: int = 300

    db_path: str = "data/bot.db"

    # Тимчасово, до Google-авторизації: Telegram ID через кому, кому дозволено надсилати чеки.
    dev_allowed_user_ids: str = ""

    @property
    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.dev_allowed_user_ids.replace(" ", "").split(",") if x}
