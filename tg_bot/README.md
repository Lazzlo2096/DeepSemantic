# Простой Telegram-бот

Python + aiogram 3. Поддерживает `/start`, `/help` и повтор текстовых сообщений.
Подписки и сбор новостей пока не реализованы.

## Запуск в Docker (рекомендуется)

Из корня проекта скопируй шаблон, если `tg_bot/.env` ещё не создан:

```powershell
Copy-Item tg_bot/.env.example tg_bot/.env
```

В `tg_bot/.env` укажи `BOT_TOKEN`, полученный у
[@BotFather](https://t.me/BotFather) через `/newbot`.
Токен передаётся контейнеру через окружение и не попадает в образ.

```powershell
docker compose up -d --build tg-bot
docker compose logs -f tg-bot
```

Команда запускает только бота, другие сервисы проекта ему не нужны.
Порты открывать не требуется: бот получает сообщения через polling.
Сервис включён в профиль `tg-bot`, поэтому обычный `docker compose up -d`
без этого профиля его не запускает.
После перезапуска Docker контейнер запускается автоматически, если он не был
остановлен вручную.

Остановка:

```powershell
docker compose stop tg-bot
```

После изменения токена повтори `docker compose up -d tg-bot`.
Не запускай локальный процесс и контейнер одновременно с одним токеном.

## Запуск (PowerShell)

Из корня проекта:

```powershell
cd tg_bot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Создай бота в [@BotFather](https://t.me/BotFather) командой `/newbot`.
Вставь полученный токен в `.env` вместо `YOUR_BOT_TOKEN_HERE`.
Не публикуй токен: `.env` исключён из Git.

```powershell
.\.venv\Scripts\python.exe bot.py
```

Открой бота в Telegram и отправь `/start`, затем любой текст.
Бот работает, пока запущен процесс. Остановка: `Ctrl+C`.
Используется polling, публичный адрес и webhook для запуска не нужны.
