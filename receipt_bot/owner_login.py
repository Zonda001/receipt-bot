"""One-time sign-in of the Drive folder owner: saves the refresh token the bot uploads photos with.

Run on the server, from the bot directory: python -m receipt_bot.owner_login
"""
import asyncio
import json
import os
import tempfile

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from receipt_bot.google_api import (
    DRIVE_URL, GoogleError, GoogleLogin, GoogleUnsure, LoginDenied, LoginExpired, _call, _json,
)

DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
OWNER_SCOPE = f"openid email {DRIVE_FILE}"
FOLDER_MIME = "application/vnd.google-apps.folder"
FOLDER_NAME = "Чеки (бот)"


class OwnerSettings(BaseSettings):
    # Only what this command needs: it runs when the rest of .env may still be empty.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      hide_input_in_errors=True)

    google_oauth_client_file: str
    google_owner_token_file: str
    drive_folder_id: str = ""


def save_token(path: str, refresh_token: str, scope: str) -> None:
    # mkstemp makes a 600 file next to the target, then a rename: a crash halfway can't spoil the working token.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".owner-token-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"refresh_token": refresh_token, "scope": scope}, f)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


async def ensure_folder(http: httpx.AsyncClient, access_token: str, folder_id: str, show) -> None:
    # drive.file only sees what the app created, so the bot can't see a folder made by hand in Drive.
    headers = {"Authorization": f"Bearer {access_token}"}
    if not folder_id:
        r = await _call(http, "drive folder", "POST", f"{DRIVE_URL}/files", params={"fields": "id"},
                        json={"name": FOLDER_NAME, "mimeType": FOLDER_MIME}, headers=headers)
        show(f"Створено папку «{FOLDER_NAME}». Впиши в .env: DRIVE_FOLDER_ID={_json(r, 'drive folder')['id']}")
        return
    try:
        r = await _call(http, "drive folder", "GET", f"{DRIVE_URL}/files/{folder_id}",
                        params={"fields": "name,mimeType,trashed,ownedByMe"}, headers=headers)
        folder = _json(r, "drive folder")
    except GoogleUnsure:
        raise  # a network blip is no reason to create a new folder
    except GoogleError as e:
        raise GoogleError(f"{e}: this account doesn't see DRIVE_FOLDER_ID; sign in as the folder owner "
                          "or clear DRIVE_FOLDER_ID to create a new folder") from e
    if folder.get("mimeType") != FOLDER_MIME or folder.get("trashed"):
        raise GoogleError("DRIVE_FOLDER_ID is not a folder or is in the trash")
    if not folder.get("ownedByMe"):  # the folder is shared with the team: seeing it doesn't mean owning it
        raise GoogleError("this account is not the owner of DRIVE_FOLDER_ID: sign in as the folder owner")
    show(f"Папка: «{folder.get('name')}».")


async def run(client_file: str, token_file: str, http: httpx.AsyncClient, show=print, folder_id: str = "") -> str:
    login = GoogleLogin(client_file, http)
    code = await login.start(scope=OWNER_SCOPE)
    show(f"1. Відкрий {code.url}\n"
         f"2. Введи код: {code.user_code}\n"
         "3. Обери акаунт, якому належить папка для фото, і дозволь доступ до файлів, створених застосунком.\n"
         f"Код дійсний {code.expires_in // 60} хв, чекаю...")
    tokens = await login.wait_for_tokens(code)
    if not tokens.get("refresh_token"):
        raise GoogleError("device token: no refresh_token")
    scope = tokens.get("scope", "")
    if DRIVE_FILE not in scope.split():  # the Drive checkbox can be unticked on the consent screen
        raise GoogleError("drive.file was not granted: run again and allow access to files")
    email = await login.email(tokens["access_token"])
    show(f"Увійшов як {email}.")
    # Before saving: a wrong account must not overwrite the working token.
    await ensure_folder(http, tokens["access_token"], folder_id, show)
    save_token(token_file, tokens["refresh_token"], scope)
    return email


async def main() -> None:
    if getattr(os, "geteuid", lambda: -1)() == 0:  # the bot runs as ubuntu and can't read a root-owned token
        raise SystemExit("Запусти від користувача бота, не через sudo.")
    settings = OwnerSettings()
    async with httpx.AsyncClient(timeout=30) as http:
        email = await run(settings.google_oauth_client_file, settings.google_owner_token_file, http,
                          folder_id=settings.drive_folder_id)
    print(f"Готово: фото вантажитимуться від імені {email}. Перезапусти бота, щоб він підхопив токен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except LoginDenied:
        raise SystemExit("Вхід скасовано.")
    except LoginExpired:
        raise SystemExit("Код прострочено, запусти ще раз.")
    except GoogleError as e:
        raise SystemExit(f"Не вийшло: {e}")
    except KeyboardInterrupt:
        raise SystemExit("Перервано.")
