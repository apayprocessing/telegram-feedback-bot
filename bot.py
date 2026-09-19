import asyncio
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
import aiosqlite

# -------------------- Конфигурация --------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))
GROUP_ID = int(os.getenv("GROUP_ID"))
DB_PATH = os.getenv("DB_PATH", "feedback.db")
ATTACHMENTS_DIR = os.getenv("ATTACHMENTS_DIR", "attachments")

# Антиспам
ANTISPAM_LIMIT = int(os.getenv("ANTISPAM_LIMIT", "5"))
ANTISPAM_WINDOW = int(os.getenv("ANTISPAM_WINDOW", "60"))  # секунд

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан в .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("feedback-bot")

session = AiohttpSession(timeout=30)
bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# Глобальное состояние
logs_topic_id: int | None = None
user_msg_times: dict[int, list[float]] = defaultdict(list)


# -------------------- База данных --------------------
async def init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS topics (
                user_id    INTEGER PRIMARY KEY,
                topic_id   INTEGER NOT NULL,
                username   TEXT,
                full_name  TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                direction TEXT NOT NULL,
                ts        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()


async def get_meta(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_meta(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
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


async def delete_topic(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM topics WHERE user_id = ?", (user_id,))
        await db.commit()


async def log_message(user_id: int, direction: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (user_id, direction) VALUES (?, ?)",
            (user_id, direction),
        )
        await db.commit()


# -------------------- Логи в тему --------------------
async def log_to_topic(text: str) -> None:
    if not logs_topic_id:
        return
    try:
        await bot.send_message(
            chat_id=GROUP_ID,
            text=f"⚠️ <b>Log</b>\n{text}",
            message_thread_id=logs_topic_id,
        )
    except Exception:
        pass


async def ensure_logs_topic() -> None:
    """Создаёт или находит тему для логов."""
    global logs_topic_id

    saved = await get_meta("logs_topic_id")
    if saved:
        try:
            logs_topic_id = int(saved)
            # Проверим, что тема ещё существует
            await bot.send_message(
                chat_id=GROUP_ID,
                text="🔄 Бот перезапущен",
                message_thread_id=logs_topic_id,
            )
            return
        except Exception:
            logger.warning("Сохранённая тема логов недоступна, создаём новую.")
            logs_topic_id = None

    try:
        topic = await bot.create_forum_topic(
            chat_id=GROUP_ID, name="🤖 Логи бота"
        )
        logs_topic_id = topic.message_thread_id
        await set_meta("logs_topic_id", str(logs_topic_id))
        await bot.send_message(
            chat_id=GROUP_ID,
            text="✅ Тема логов создана. Сюда будут приходить ошибки бота.",
            message_thread_id=logs_topic_id,
        )
    except Exception:
        logger.exception("Не удалось создать тему логов")


# -------------------- Антиспам --------------------
def is_spam(user_id: int) -> bool:
    now = time.time()
    times = user_msg_times[user_id]
    # Убираем старые
    times[:] = [t for t in times if now - t < ANTISPAM_WINDOW]
    if len(times) >= ANTISPAM_LIMIT:
        return True
    times.append(now)
    return False


# -------------------- Сохранение вложений --------------------
async def save_attachment(message: Message, user_id: int) -> str | None:
    try:
        file_id = None
        name = None

        if message.photo:
            file_id = message.photo[-1].file_id
            name = f"{message.message_id}_photo.jpg"
        elif message.document:
            file_id = message.document.file_id
            name = f"{message.message_id}_{message.document.file_name or 'file'}"
        elif message.video:
            file_id = message.video.file_id
            name = f"{message.message_id}_video.mp4"
        elif message.voice:
            file_id = message.voice.file_id
            name = f"{message.message_id}_voice.ogg"
        elif message.audio:
            file_id = message.audio.file_id
            name = f"{message.message_id}_audio.mp3"
        elif message.video_note:
            file_id = message.video_note.file_id
            name = f"{message.message_id}_videonote.mp4"
        elif message.sticker:
            file_id = message.sticker.file_id
            name = f"{message.message_id}_sticker.webp"
        else:
            return None

        user_dir = os.path.join(ATTACHMENTS_DIR, str(user_id))
        os.makedirs(user_dir, exist_ok=True)
        path = os.path.join(user_dir, name)

        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, path)
        return path
    except Exception:
        logger.exception("Не удалось сохранить вложение")
        return None


# -------------------- Пользователь --------------------
@dp.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Здравствуйте!\n\n"
        "Напишите ваше сообщение, и оно попадёт оператору. "
        "Ответ придёт сюда же, в этот чат.\n\n"
        "Можно отправлять текст, фото, видео, документы и голосовые."
    )


@dp.message(F.chat.type == ChatType.PRIVATE, ~F.text.startswith("/"))
async def user_message(message: Message) -> None:
    user = message.from_user

    # Антиспам
    if is_spam(user.id):
        await message.answer(
            "⏳ Слишком много сообщений. Подождите минуту и попробуйте снова."
        )
        return

    topic_id = await get_topic_by_user(user.id)

    # Создаём тему, если её нет
    if topic_id is None:
        topic_name = f"{user.full_name} | {user.id}"
        try:
            topic = await bot.create_forum_topic(
                chat_id=GROUP_ID,
                name=topic_name[:128],
            )
            topic_id = topic.message_thread_id
        except Exception:
            logger.exception("Не удалось создать тему")
            await message.answer("⚠️ Не удалось создать обращение. Попробуйте позже.")
            return

        await save_topic(
            user_id=user.id,
            topic_id=topic_id,
            username=user.username or "",
            full_name=user.full_name,
        )

        # Шапка темы с кнопкой закрытия
        header = f"👤 <b>{user.full_name}</b>\n"
        if user.username:
            header += f"🔗 @{user.username}\n"
        header += f"🆔 <code>{user.id}</code>"

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔒 Закрыть обращение",
                        callback_data=f"close:{user.id}",
                    )
                ]
            ]
        )

        try:
            await bot.send_message(
                chat_id=GROUP_ID,
                message_thread_id=topic_id,
                text=header,
                reply_markup=kb,
            )
        except Exception:
            logger.exception("Не удалось отправить шапку темы")

    # Сохраняем вложение, если есть
    await save_attachment(message, user.id)

    # Пересылаем сообщение в тему
    try:
        await message.forward(
            chat_id=GROUP_ID,
            message_thread_id=topic_id,
        )
    except Exception:
        try:
            await bot.copy_message(
                chat_id=GROUP_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                message_thread_id=topic_id,
            )
        except Exception:
            logger.exception("Не удалось переслать сообщение")
            await log_to_topic(
                f"Не удалось переслать сообщение от {user.id}: {message.message_id}"
            )
            await message.answer(
                "⚠️ Не удалось отправить сообщение. Попробуйте ещё раз."
            )
            return

    await log_message(user.id, "in")
    await message.answer("✅ Сообщение отправлено оператору.")


# -------------------- Владелец --------------------
@dp.message(F.chat.id == GROUP_ID, F.from_user.id == OWNER_ID, Command("stats"))
async def cmd_stats(message: Message) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM topics") as cur:
            total_users = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE direction = 'in'"
        ) as cur:
            total_in = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE direction = 'out'"
        ) as cur:
            total_out = (await cur.fetchone())[0]

        today = datetime.utcnow().date().isoformat()
        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE direction = 'in' AND date(ts) = ?",
            (today,),
        ) as cur:
            today_in = (await cur.fetchone())[0]

    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей с открытыми темами: <b>{total_users}</b>\n"
        f"📥 Сообщений от пользователей: <b>{total_in}</b>\n"
        f"📤 Ответов оператора: <b>{total_out}</b>\n"
        f"📅 Сегодня входящих: <b>{today_in}</b>"
    )
    await message.reply(text)


@dp.message(F.chat.id == GROUP_ID, F.from_user.id == OWNER_ID, Command("help"))
async def cmd_help(message: Message) -> None:
    await message.reply(
        "Доступные команды:\n"
        "/stats — статистика\n"
        "/help — эта справка\n\n"
        "Ответить пользователю — просто Reply на пересланное сообщение в теме."
    )


@dp.callback_query(F.data.startswith("close:"))
async def close_topic_cb(cb: CallbackQuery) -> None:
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Только владелец может закрыть обращение", show_alert=True)
        return

    user_id = int(cb.data.split(":", 1)[1])
    topic_id = await get_topic_by_user(user_id)

    if topic_id is None:
        await cb.answer("Тема уже закрыта или не найдена")
        return

    try:
        await bot.delete_forum_topic(
            chat_id=GROUP_ID, message_thread_id=topic_id
        )
    except Exception:
        logger.exception("Не удалось удалить тему")
        await log_to_topic(f"Не удалось удалить тему {topic_id}")

    await delete_topic(user_id)
    await cb.answer("Обращение закрыто")
    await log_to_topic(f"Владелец закрыл обращение пользователя {user_id}")


@dp.message(F.chat.id == GROUP_ID, F.from_user.id == OWNER_ID)
async def owner_reply(message: Message) -> None:
    if message.reply_to_message is None:
        return

    thread_id = message.message_thread_id
    if thread_id is None:
        return

    # Игнорируем сообщения из темы логов
    if logs_topic_id and thread_id == logs_topic_id:
        return

    user_id = await get_user_by_topic(thread_id)
    if user_id is None:
        return

    try:
        await message.copy_to(chat_id=user_id)
        await message.reply("✅ Отправлено пользователю.")
        await log_message(user_id, "out")
    except Exception:
        logger.exception("Не удалось отправить ответ пользователю")
        await message.reply(
            "⚠️ Не удалось отправить ответ. Возможно, пользователь заблокировал бота."
        )
        await log_to_topic(
            f"Не доставлено сообщение пользователю {user_id}"
        )


# -------------------- Точка входа --------------------
async def set_commands() -> None:
    await bot.set_my_commands(
        [BotCommand(command="start", description="Начать диалог")]
    )


async def main() -> None:
    await init_db()
    logger.info("Бот запускается...")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.warning("Не удалось сбросить вебхук, продолжаем.")

    await set_commands()
    await ensure_logs_topic()

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
