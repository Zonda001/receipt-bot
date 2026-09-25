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

    # Vision-модель, будь-який OpenAI-сумісний API; готові блоки — у .env.example.
    # "Без міркувань" у кожного провайдера своє, тож за замовчуванням не шлемо нічого: чуже поле = 400.
    llm_base_url: str
    llm_model: str
    llm_api_key: SecretStr
    llm_reasoning_effort: str = ""   # Groq: none
    llm_extra_body: str = ""         # JSON; Cloudflare: {"chat_template_kwargs": {"enable_thinking": false}}

    # Запасний провайдер; порожній base_url — вимкнено.
    llm_fallback_base_url: str = ""
    llm_fallback_model: str = ""
    llm_fallback_api_key: SecretStr = SecretStr("")
    llm_fallback_reasoning_effort: str = ""
    llm_fallback_extra_body: str = ""

    # Запобіжник на добу на всю команду. Стелі free-тарифів: Cloudflare ~1100 чеків, Groq ~80.
    daily_recognitions: int = 300

    db_path: str = "data/bot.db"

    # Тимчасово, до Google-авторизації: Telegram ID через кому, кому дозволено надсилати чеки.
    dev_allowed_user_ids: str = ""

    @property
    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.dev_allowed_user_ids.replace(" ", "").split(",") if x}
