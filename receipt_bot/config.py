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

    llm_base_url: str
    llm_model: str
    llm_api_key: SecretStr
    # Groq/OpenAI приймають "none" (без міркувань, швидше); порожньо — параметр не надсилається.
    llm_reasoning_effort: str = "none"
    # Стеля розпізнавань на добу на всю команду (Groq free: ~200K токенів/добу, ~1.5-2K на чек).
    daily_recognitions: int = 100

    db_path: str = "data/bot.db"

    # Тимчасово, до Google-авторизації: Telegram ID через кому, кому дозволено надсилати чеки.
    dev_allowed_user_ids: str = ""

    @property
    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.dev_allowed_user_ids.replace(" ", "").split(",") if x}
