import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
import aiosqlite

# -------------------- Конфигурация --------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))
GROUP_ID = int(os.getenv("GROUP_ID"))
DB_PATH = os.getenv("DB_PATH", "feedback.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("feedback-bot")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


# -------------------- База данных --------------------
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topics (
                user_id   INTEGER PRIMARY KEY,
                topic_id  INTEGER NOT NULL,
                username  TEXT,
                full_name TEXT
            )
            """
        )
        await db.commit()


async def get_topic_by_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT topic_id FROM topics WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_user_by_topic(topic_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM topics WHERE topic_id = ?", (topic_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def save_topic(user_id: int, topic_id: int, username: str, full_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO topics (user_id, topic_id, username, full_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                topic_id  = excluded.topic_id,
                username  = excluded.username,
                full_name = excluded.full_name
            """,
            (user_id, topic_id, username, full_name),
        )
        await db.commit()


# -------------------- Хендлеры: пользователь --------------------
@dp.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Здравствуйте!\n\n"
        "Напишите ваше сообщение, и оно попадёт оператору. "
        "Ответ придёт сюда же, в этот чат."
    )


@dp.message(F.chat.type == ChatType.PRIVATE, ~F.text.startswith("/"))
async def user_message(message: Message) -> None:
    user = message.from_user

    # 1. Находим или создаём тему для этого пользователя
    topic_id = await get_topic_by_user(user.id)

    if topic_id is None:
        topic_name = f"{user.full_name} | {user.id}"
        try:
            topic = await bot.create_forum_topic(
                chat_id=GROUP_ID,
                name=topic_name[:128],  # ограничение Telegram
            )
            topic_id = topic.message_thread_id
        except Exception:
            logger.exception("Не удалось создать тему")
            await message.answer(
                "⚠️ Не удалось создать обращение. Попробуйте позже."
            )
            return

        await save_topic(
            user_id=user.id,
            topic_id=topic_id,
            username=user.username or "",
            full_name=user.full_name,
        )

        # Шапка темы с информацией о пользователе
        header = f"👤 <b>{user.full_name}</b>\n"
        if user.username:
            header += f"🔗 @{user.username}\n"
        header += f"🆔 <code>{user.id}</code>"

        try:
            await bot.send_message(
                chat_id=GROUP_ID,
                message_thread_id=topic_id,
                text=header,
            )
        except Exception:
            logger.exception("Не удалось отправить шапку темы")

    # 2. Пересылаем сообщение пользователя в тему
    try:
        await message.forward(
            chat_id=GROUP_ID,
            message_thread_id=topic_id,
        )
    except Exception:
        # Если пересылка запрещена — копируем
        try:
            await bot.copy_message(
                chat_id=GROUP_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                message_thread_id=topic_id,
            )
        except Exception:
            logger.exception("Не удалось переслать сообщение")
            await message.answer("⚠️ Не удалось отправить сообщение. Попробуйте ещё раз.")
            return

    await message.answer("✅ Сообщение отправлено оператору.")


# -------------------- Хендлеры: владелец в группе --------------------
@dp.message(F.chat.id == GROUP_ID, F.from_user.id == OWNER_ID)
async def owner_reply(message: Message) -> None:
    # Отвечаем пользователю только если владелец ответил через Reply
    if message.reply_to_message is None:
        return

    thread_id = message.message_thread_id
    if thread_id is None:
        return

    user_id = await get_user_by_topic(thread_id)
    if user_id is None:
        return

    try:
        await message.copy_to(chat_id=user_id)
        await message.reply("✅ Отправлено пользователю.")
    except Exception:
        logger.exception("Не удалось отправить ответ пользователю")
        await message.reply("⚠️ Не удалось отправить ответ. Проверьте, не заблокировал ли бота пользователь.")


# -------------------- Точка входа --------------------
async def main() -> None:
    await init_db()
    logger.info("Бот запускается...")

    # Убираем возможный вебхук, чтобы polling работал стабильно
    await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
