# event_bot_v3.py
import asyncio
import aiosqlite
import datetime
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatInviteLink,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# ============ КОНФИГ ============
BOT_TOKEN = "8574726641:AAEMhFafzQs8HIzqWGaNv0cgaERyIRIZRsI"
EVENTERS_CHAT_ID = -1003339498144   # чат ивентеров (где работают команды)
CHANNEL_ID = -1003484242724         # канал, куда бот постит анонсы
OWNER_ID = 7504103313                # твой Telegram ID
ADMINS = {OWNER_ID, 111111111, 222222222}  # админы с доступом к /invite и панели

DB_PATH = "events_v3.db"

# ============ HELPERS ============
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            creator_id INTEGER NOT NULL,
            creator_username TEXT,
            channel_msg_id INTEGER,
            chat_msg_id INTEGER,
            scheduled_at TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS signups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            joined_at TEXT NOT NULL,
            UNIQUE(event_id, user_id)
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            kind TEXT,
            text TEXT
        );
        """)
        await db.commit()

async def log(kind: str, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO logs(ts, kind, text) VALUES(?, ?, ?)",
            (datetime.datetime.utcnow().isoformat(), kind, text)
        )
        await db.commit()

def only_eventers_chat(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Allow callbacks (CallbackQuery) queries too
        chat_id = None
        if update.effective_chat:
            chat_id = update.effective_chat.id
        elif update.callback_query and update.callback_query.message:
            chat_id = update.callback_query.message.chat.id

        if chat_id != EVENTERS_CHAT_ID:
            # If command used in private or elsewhere -> notify (but minimal)
            if update.effective_message:
                await update.effective_message.reply_text("❌ Команды доступны только в чате ивентеров.")
            return
        return await func(update, context)
    return wrapper

# ============ HANDLERS ============

@only_eventers_chat
async def create_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/event_create <название>  - создаёт ивент немедленно (пост в канале)"""
    if not context.args:
        await update.message.reply_text("Использование:\n/event_create <название ивента>")
        return

    name = " ".join(context.args).strip()
    creator = update.effective_user
    now = datetime.datetime.utcnow().isoformat()

    # Кнопки: записаться / участники
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Записаться", callback_data=f"signup:{name}")],
        [InlineKeyboardButton("📋 Участники", callback_data=f"list:{name}")]
    ])

    text = (
        f"🎉 *ИВЕНТ СТАРТУЕТ!*\n\n"
        f"🕹 *{name}*\n"
        f"👤 Ведёт: @{creator.username if creator.username else creator.full_name}\n"
        f"⏰ Начало: прямо сейчас\n\n"
        f"Нажмите кнопку, чтобы записаться."
    )

    sent = await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard
    )

    # Сохраняем в БД
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO events(name, creator_id, creator_username, channel_msg_id, chat_msg_id, scheduled_at, active, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (name, creator.id, creator.username or creator.full_name, sent.message_id, None, None, 1, now)
        )
        await db.commit()
        event_id = cur.lastrowid

    await log("create_event", f"{name} by {creator.id}")
    await update.message.reply_text(f"Ивент *{name}* создан и отправлен в канал.", parse_mode="Markdown")

@only_eventers_chat
async def start_event_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start_event <название> - алиас для /event_create"""
    await create_event(update, context)

@only_eventers_chat
async def end_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/end_event <название or event_id> - завершает ивент (помечает inactive)"""
    if not context.args:
        await update.message.reply_text("Использование:\n/end_event <название или ID>")
        return

    key = " ".join(context.args).strip()
    async with aiosqlite.connect(DB_PATH) as db:
        # попытаемся сначала по id
        row = None
        try:
            eid = int(key)
            cur = await db.execute("SELECT id, name FROM events WHERE id=? AND active=1", (eid,))
            row = await cur.fetchone()
        except:
            cur = await db.execute("SELECT id, name FROM events WHERE name LIKE ? AND active=1 ORDER BY id DESC", (f"%{key}%",))
            row = await cur.fetchone()

        if not row:
            await update.message.reply_text("Ивент не найден или уже завершён.")
            return

        event_id, name = row
        await db.execute("UPDATE events SET active=0 WHERE id=?", (event_id,))
        await db.commit()

    await context.bot.send_message(chat_id=CHANNEL_ID, text=f"🔔 Ивент *{name}* завершён.", parse_mode="Markdown")
    await log("end_event", f"{name} by {update.effective_user.id}")
    await update.message.reply_text(f"Ивент *{name}* завершён.", parse_mode="Markdown")

@only_eventers_chat
async def schedule_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /schedule <YYYY-MM-DD> <HH:MM> <название>
    пример: /schedule 2025-11-28 20:30 Имаджинариум
    """
    if len(context.args) < 3:
        await update.message.reply_text("Использование:\n/schedule <YYYY-MM-DD> <HH:MM> <название>")
        return

    date_str = context.args[0]
    time_str = context.args[1]
    name = " ".join(context.args[2:]).strip()
    try:
        dt = datetime.datetime.fromisoformat(f"{date_str}T{time_str}")
    except Exception as e:
        await update.message.reply_text("Неверный формат даты/времени. Формат: 2025-11-28 20:30")
        return

    now = datetime.datetime.utcnow()
    # store scheduled as ISO in UTC (assume user provides UTC or server timezone - note for deploy)
    scheduled_iso = dt.isoformat()

    creator = update.effective_user
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO events(name, creator_id, creator_username, created_at, scheduled_at, active) VALUES(?,?,?,?,?,1)",
            (name, creator.id, creator.username or creator.full_name, datetime.datetime.utcnow().isoformat(), scheduled_iso)
        )
        await db.commit()
        event_id = cur.lastrowid

    # Планируем задачу через JobQueue
    context.job_queue.run_once(run_scheduled_event, when=(dt - now).total_seconds(), data={"event_id": event_id}, name=f"event_{event_id}")

    await log("schedule_event", f"{name} at {scheduled_iso} by {creator.id}")
    await update.message.reply_text(f"Ивент *{name}* запланирован на {scheduled_iso}", parse_mode="Markdown")

async def run_scheduled_event(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data or {}
    event_id = data.get("event_id")
    if not event_id:
        return
    # пометить и опубликовать
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT name, creator_username FROM events WHERE id=? AND active=1", (event_id,))
        row = await cur.fetchone()
        if not row:
            return
        name, creator_username = row
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Записаться", callback_data=f"signup_id:{event_id}")],
            [InlineKeyboardButton("📋 Участники", callback_data=f"list_id:{event_id}")]
        ])
        text = (
            f"📅 *Запланированный ивент стартует!*\n\n"
            f"🕹 *{name}*\n"
            f"👤 Ведёт: @{creator_username}\n"
            f"⏰ Начало: прямо сейчас\n\n"
            f"Нажмите кнопку, чтобы записаться."
        )
        sent = await context.bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="Markdown", reply_markup=keyboard)
        # обновляем запись
        await db.execute("UPDATE events SET channel_msg_id=? WHERE id=?", (sent.message_id, event_id))
        await db.commit()
        await log("run_scheduled_event", f"{name} id={event_id}")

# ========== Invite ==========
@only_eventers_chat
async def invite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/invite - админы создают одноразовый инвайт в чат ивентеров, отправляется владельцу"""
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ У вас нет прав.")
        return

    invite: ChatInviteLink = await context.bot.create_chat_invite_link(
        chat_id=EVENTERS_CHAT_ID,
        member_limit=1,
        creates_join_request=False
    )

    # отправляем владельцу в личку
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=f"🔗 Инвайт в чат ивентеров (1-пользователь):\n{invite.invite_link}")
        await update.message.reply_text("Инвайт создан и отправлен владельцу.")
        await log("create_invite", f"by {user_id}")
    except Exception as e:
        await update.message.reply_text("Не удалось отправить инвайт владельцу. Проверь настройки.")
        await log("error_invite", str(e))

# ========== SIGNUP / CALLBACKS ==========
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Форматы:
    # signup:<name>
    # signup_id:<event_id>
    # list:<name>
    # list_id:<event_id>
    if data.startswith("signup_id:"):
        eid = int(data.split(":",1)[1])
        await signup_by_id(eid, query, context)
    elif data.startswith("signup:"):
        name = data.split(":",1)[1]
        await signup_by_name(name, query, context)
    elif data.startswith("list_id:"):
        eid = int(data.split(":",1)[1])
        await list_participants_by_id(eid, query, context)
    elif data.startswith("list:"):
        name = data.split(":",1)[1]
        await list_participants_by_name(name, query, context)
    else:
        await query.edit_message_text("Неизвестная кнопка.")

async def signup_by_id(eid: int, query, context):
    user = query.from_user
    now = datetime.datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("INSERT INTO signups(event_id, user_id, username, joined_at) VALUES(?,?,?,?)",
                             (eid, user.id, user.username or user.full_name, now))
            await db.commit()
            await query.answer("Вы записаны ✅", show_alert=False)
            await log("signup", f"event {eid} user {user.id}")
        except aiosqlite.IntegrityError:
            # уже есть
            await query.answer("Вы уже записаны.", show_alert=False)

async def signup_by_name(name: str, query, context):
    # Найдём последний активный ивент с таким именем
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM events WHERE name LIKE ? AND active=1 ORDER BY id DESC", (f"%{name}%",))
        row = await cur.fetchone()
        if not row:
            await query.answer("Ивент не найден.", show_alert=True)
            return
        eid = row[0]
    await signup_by_id(eid, query, context)

async def list_participants_by_id(eid: int, query, context):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT username, joined_at FROM signups WHERE event_id=? ORDER BY joined_at", (eid,))
        rows = await cur.fetchall()
        if not rows:
            await query.answer("Нет участников.", show_alert=True)
            return
        text = "Участники:\n" + "\n".join([f"- {r[0]}" for r in rows])
        # Показываем в модальном окне
        await query.answer(text, show_alert=True)

async def list_participants_by_name(name: str, query, context):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM events WHERE name LIKE ? AND active=1 ORDER BY id DESC", (f"%{name}%",))
        row = await cur.fetchone()
        if not row:
            await query.answer("Ивент не найден.", show_alert=True)
            return
        eid = row[0]
    await list_participants_by_id(eid, query, context)

# ========== ADMIN PANEL ==========
@only_eventers_chat
async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/panel - кнопки управления (только для админов)"""
    uid = update.effective_user.id
    if uid not in ADMINS:
        await update.message.reply_text("❌ Только для админов.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 Список активных ивентов", callback_data="panel_list")],
        [InlineKeyboardButton("📥 Логи (последние 10)", callback_data="panel_logs")],
    ])
    await update.message.reply_text("Панель админа:", reply_markup=keyboard)

async def panel_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMINS:
        await query.edit_message_text("❌ Только для админов.")
        return

    if query.data == "panel_list":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT id, name, scheduled_at FROM events WHERE active=1 ORDER BY id DESC")
            rows = await cur.fetchall()
            if not rows:
                await query.edit_message_text("Активных ивентов нет.")
                return
            text = "Активные ивенты:\n" + "\n".join([f"{r[0]} — {r[1]} — {r[2] or 'не запланирован'}" for r in rows])
            await query.edit_message_text(text)
    elif query.data == "panel_logs":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT ts, kind, text FROM logs ORDER BY id DESC LIMIT 10")
            rows = await cur.fetchall()
            if not rows:
                await query.edit_message_text("Логов нет.")
                return
            text = "Логи (последние 10):\n" + "\n".join([f"{r[0]} [{r[1]}] {r[2]}" for r in rows])
            await query.edit_message_text(text)

# ========== UTILS ==========
async def list_events_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list_events — список активных ивентов"""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name, scheduled_at FROM events WHERE active=1 ORDER BY id DESC")
        rows = await cur.fetchall()
        if not rows:
            await update.message.reply_text("Активных ивентов нет.")
            return
        text = "Активные ивенты:\n" + "\n".join([f"{r[0]} — {r[1]} — {r[2] or 'не запланирован'}" for r in rows])
        await update.message.reply_text(text)

# ========== START / PRIVATE BLOCK ==========
async def block_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Блокируем команды в ЛС (если пользователь напишет боту в ЛС)
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ Бот работает только в чате ивентеров.")
    else:
        await update.message.reply_text("Привет! Используйте команды в чате ивентеров.")

# ========== MAIN ==========
async def main():
    await init_db()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("event_create", create_event))
    app.add_handler(CommandHandler("start_event", start_event_now))
    app.add_handler(CommandHandler("end_event", end_event))
    app.add_handler(CommandHandler("schedule", schedule_event))
    app.add_handler(CommandHandler("invite", invite_cmd))
    app.add_handler(CommandHandler("panel", panel))
    app.add_handler(CommandHandler("list_events", list_events_cmd))
    app.add_handler(CommandHandler("start", block_private))

    app.add_handler(CallbackQueryHandler(callback_router, pattern="^(signup:|signup_id:|list:|list_id:)"))
    app.add_handler(CallbackQueryHandler(panel_router, pattern="^panel_"))

    # JobQueue уже есть в Application; запустим приложение
    await app.start()
    print("Bot started")
    await app.updater.start_polling()  # совместимо с v20; если не работает, вместо этого можно app.run_polling()
    # keep running
    await app.idle()

if __name__ == "__main__":
    asyncio.run(main())
