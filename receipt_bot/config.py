from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Усі налаштування з .env. SecretStr не друкує значення в логах і repr."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    bot_token: SecretStr

    google_sa_key_file: str
    google_oauth_client_file: str
    sheet_id: str

    drive_upload_url: SecretStr
    drive_upload_secret: SecretStr

    llm_base_url: str
    llm_model: str
    llm_api_key: SecretStr

    db_path: str = "data/bot.db"
