import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv


dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Привет! Я простой бот. Напиши мне текст, и я повторю его.\n"
        "Подписки на новости появятся позже.\n"
        "Справка: /help"
    )


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer("/start — приветствие\n/help — справка\nОтправь текст — я повторю его.")


@dp.message(F.text)
async def echo(message: Message) -> None:
    await message.answer(message.text)


async def main() -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Укажи BOT_TOKEN в файле tg_bot/.env. Токен можно получить у @BotFather.")

    # Не выводим URL запросов и токен в логи.
    logging.basicConfig(level=logging.INFO)
    async with Bot(token=token) as bot:
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
