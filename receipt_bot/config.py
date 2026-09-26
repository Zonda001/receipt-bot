from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings come from .env. SecretStr keeps values out of logs and repr."""

    # hide_input_in_errors: otherwise pydantic prints the bad .env value (tokens!) to the journal.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", hide_input_in_errors=True)

    bot_token: SecretStr

    google_sa_key_file: str
    google_oauth_client_file: str
    sheet_id: str

    google_owner_token_file: str
    drive_folder_id: str

    # Vision model, any OpenAI-compatible API; ready-made blocks are in .env.example.
    # Every provider turns off "thinking" its own way, so by default we send nothing: an unknown field = 400.
    llm_base_url: str
    llm_model: str
    llm_api_key: SecretStr
    llm_reasoning_effort: str = ""   # Groq: none
    llm_extra_body: str = ""         # JSON; Cloudflare: {"chat_template_kwargs": {"enable_thinking": false}}

    # Fallback provider; an empty base_url turns it off.
    llm_fallback_base_url: str = ""
    llm_fallback_model: str = ""
    llm_fallback_api_key: SecretStr = SecretStr("")
    llm_fallback_reasoning_effort: str = ""
    llm_fallback_extra_body: str = ""

    # Daily safety cap for the whole team. Free-tier ceilings: Cloudflare ~1100 receipts, Groq ~80.
    daily_recognitions: int = 300

    db_path: str = "data/bot.db"  # which Telegram user signed in with which Google account
