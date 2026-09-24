# receipt-bot

Telegram-бот для командного обліку чеків: фото -> розпізнана сума -> підтвердження -> Google Drive + Google Sheets.

> Чернетка. Повний README (запуск, авторизація, архітектура, тестування) — в кінці роботи.

## Запуск (локально)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # заповнити значення
python -m receipt_bot
```
