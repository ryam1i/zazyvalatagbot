from __future__ import annotations
import os
from typing import Optional, NamedTuple
import sqlite3
import logging
import random
import emoji
import time
import re
import html
import asyncio
from functools import wraps
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()

try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "").strip())
except ValueError:
    ADMIN_ID = None

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    LinkPreviewOptions
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    PreCheckoutQueryHandler,
    filters,
    ContextTypes
)
from telegram.error import RetryAfter, Forbidden, BadRequest

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

DB_FILE = "users.db"
BACKUP_DIR = "backups"
COOLDOWN_SECONDS = 60
ALL_EMOJIS = list(emoji.EMOJI_DATA.keys())
USER_COOLDOWNS: dict[int, float] = {}
BUTTON_COOLDOWNS: dict[int, float] = {}
TRACKED_USERS_CACHE: dict[tuple[int, int], float] = {}

class ChatMember(NamedTuple):
    user_id: int
    username: Optional[str]
    first_name: Optional[str]
    emoji: Optional[str] = None

@contextmanager
def db_cursor(db_file: str = DB_FILE):
    conn = sqlite3.connect(db_file, timeout=20)
    try:
        with conn:
            yield conn.cursor()
    finally:
        conn.close()

async def run_db(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

def init_db() -> None:
    with db_cursor() as cur:
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA busy_timeout=10000;")
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER,
                user_id INTEGER,
                username TEXT,
                first_name TEXT,
                emoji TEXT,
                is_unregistered INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        ''')
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_users_chat_active
            ON users (chat_id, is_unregistered)
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                language TEXT,
                chat_type TEXT DEFAULT 'group'
            )
        ''')
        cur.execute("PRAGMA table_info(chats);")
        cols = [col[1] for col in cur.fetchall()]
        if "chat_type" not in cols:
            cur.execute("ALTER TABLE chats ADD COLUMN chat_type TEXT DEFAULT 'group';")
            cur.execute("UPDATE chats SET chat_type = 'private' WHERE chat_id > 0;")

def upsert_user(chat_id: int, user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    with db_cursor() as cur:
        cur.execute('''
            INSERT INTO users (chat_id, user_id, username, first_name, is_unregistered)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                is_unregistered = 0
        ''', (chat_id, user_id, username, first_name))

def get_user_batches(chat_id: int, batch_size: int = 5) -> list[list[ChatMember]]:
    with db_cursor() as cur:
        cur.execute(
            'SELECT user_id, username, first_name, emoji FROM users WHERE chat_id = ? AND (is_unregistered IS NULL OR is_unregistered = 0)',
            (chat_id,)
        )
        all_rows = cur.fetchall()
        members = [ChatMember(*row) for row in all_rows]
        return [members[i:i + batch_size] for i in range(0, len(members), batch_size)]

def set_user_emoji(chat_id: int, user_id: int, emoji_str: str) -> None:
    with db_cursor() as cur:
        cur.execute('UPDATE users SET emoji = ? WHERE chat_id = ? AND user_id = ?', (emoji_str, chat_id, user_id))

def is_emoji_taken_by_others(chat_id: int, user_id: int, emoji_str: str) -> bool:
    with db_cursor() as cur:
        res = cur.execute('SELECT 1 FROM users WHERE chat_id = ? AND emoji = ? AND user_id != ? AND (is_unregistered IS NULL OR is_unregistered = 0)', (chat_id, emoji_str, user_id)).fetchone()
        return bool(res)

def remove_user(chat_id: int, user_id: int) -> None:
    with db_cursor() as cur:
        cur.execute('UPDATE users SET is_unregistered = 1 WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))

def delete_user(chat_id: int, user_id: int) -> None:
    with db_cursor() as cur:
        cur.execute('DELETE FROM users WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))

def update_chat_title(chat_id: int, title: str, chat_type: Optional[str] = None) -> None:
    with db_cursor() as cur:
        cur.execute('''
            INSERT INTO chats (chat_id, title, chat_type)
            VALUES (?, ?, COALESCE(?, 'group'))
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                chat_type = COALESCE(excluded.chat_type, chats.chat_type)
        ''', (chat_id, title, chat_type))

def delete_chat(chat_id: int) -> None:
    with db_cursor() as cur:
        cur.execute('DELETE FROM chats WHERE chat_id = ?', (chat_id,))
        cur.execute('DELETE FROM users WHERE chat_id = ?', (chat_id,))

def migrate_chat_id(old_chat_id: int, new_chat_id: int) -> None:
    with db_cursor() as cur:
        cur.execute('UPDATE chats SET chat_id = ?, chat_type = "supergroup" WHERE chat_id = ?', (new_chat_id, old_chat_id))
        cur.execute('UPDATE users SET chat_id = ? WHERE chat_id = ?', (new_chat_id, old_chat_id))

def get_all_group_chats() -> list[int]:
    with db_cursor() as cur:
        rows = cur.execute('SELECT chat_id FROM chats WHERE chat_id < 0').fetchall()
        return [row[0] for row in rows]

def get_chat_language(chat_id: int) -> Optional[str]:
    with db_cursor() as cur:
        res = cur.execute('SELECT language FROM chats WHERE chat_id = ?', (chat_id,)).fetchone()
        return res[0] if res else None

def set_chat_language(chat_id: int, lang_str: str, title: Optional[str] = None, chat_type: Optional[str] = None) -> None:
    with db_cursor() as cur:
        cur.execute('''
            INSERT INTO chats (chat_id, language, title, chat_type)
            VALUES (?, ?, ?, COALESCE(?, 'group'))
            ON CONFLICT(chat_id) DO UPDATE SET
                language = excluded.language,
                title = COALESCE(excluded.title, chats.title),
                chat_type = COALESCE(excluded.chat_type, chats.chat_type)
        ''', (chat_id, lang_str, title, chat_type))

def get_cached_chat_language(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    if "lang" in context.chat_data:
        return context.chat_data["lang"]
    lang = get_chat_language(chat_id)
    if lang:
        context.chat_data["lang"] = lang
    return lang

def get_user_or_chat_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    chat_id = update.effective_chat.id if update.effective_chat else None
    lang = get_cached_chat_language(chat_id, context) if chat_id else None
    if not lang:
        user_lang = update.effective_user.language_code if update.effective_user else None
        lang = "ru" if user_lang and user_lang.startswith("ru") else "en"
    return lang

def is_admin(user_id: int) -> bool:
    return ADMIN_ID is not None and user_id == ADMIN_ID

def backup_db(backup_dir: str = BACKUP_DIR) -> Optional[str]:
    try:
        if not os.path.exists(DB_FILE):
            return None
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"users_backup_{timestamp}.db")

        with sqlite3.connect(DB_FILE) as src_conn:
            with sqlite3.connect(backup_path) as dst_conn:
                src_conn.backup(dst_conn)

        backup_files = sorted([
            os.path.join(backup_dir, f) for f in os.listdir(backup_dir)
            if f.startswith("users_backup_") and f.endswith(".db")
        ])
        while len(backup_files) > 5:
            oldest = backup_files.pop(0)
            try:
                os.remove(oldest)
            except OSError:
                pass

        logger.info(f"Database backup created: {backup_path}")
        return backup_path
    except Exception as e:
        logger.error(f"Error creating database backup: {e}")
        return None

TRANSLATIONS = {
    "en": {
        "welcome": (
            "👋 *Welcome! I'm a bot designed to easily tag active members in your groups.*\n\n"
            "Here's how to use me:\n"
            "• Type `/call` (or `call`) to tag everyone (with random or personal emojis).\n"
            "• Type `/call your message` to tag everyone along with your specific message.\n"
            "• Use `/all` to trigger a generic group tag.\n"
            "• Use `/setme <emoji>` to claim a personal emoji for yourself!\n"
            "• Use `/me` to check what emoji you have installed!\n"
            "• Use `/unreg` or type `unreg` to temporarily opt-out of being tagged.\n"
            "• Use `/language` to choose your language.\n\n"
            "Add me to a group, make sure everyone sends at least one message so I can track them, and you're good to go!"
        ),
        "group_only": "This command can only be used in groups. / Эту команду можно использовать только в группах.",
        "lang_not_set": "Please choose a language first using /language. / Пожалуйста, сначала выберите язык с помощью команды /language.",
        "unreg_success": "You have been excluded from tagging. (You will be added back if you send another message).",
        "cooldown": "Wait {remaining} seconds before trying again.",
        "no_active_users": "I haven't seen anyone talk yet. People need to send messages before I can tag them!",
        "setme_usage": "Please provide an emoji. Usage: /setme <emoji>",
        "invalid_emoji": "That doesn't look like a valid emoji.",
        "emoji_taken": "Sorry, this one is already taken.",
        "setme_success": "Done! Now your emoji is set to: {emoji}",
        "me_success": "Your installed emoji is: {emoji}",
        "me_none": "You don't have an emoji installed. Use /setme <emoji> to set one.",
        "lang_choose": "Please choose your language:",
        "lang_success": "Language successfully set to English! 🇬🇧",
        "rate_limit_stop": "⚠️ Too many messages. Telegram rate limit reached, mentions paused. Please try again later.",
        "support_btn": "❤️ Support Zazyvala",
        "support_text": (
            "❤️ <b>Support the Project</b>\n\n"
            'This bot is free, <a href="https://github.com/ryam1i/zazyvalatagbot">open-source</a>, and contains no ads or telemetry. '
            "It is developed and maintained entirely on enthusiasm.\n\n"
            "If you'd like to support server maintenance and future development, "
            "you can send Telegram Stars ⭐ below:\n\n"
            "<i>Want to donate more? Simply reply to this message with any number of Stars (e.g. 150 or 500).</i>"
        ),
        "support_stars_25": "⭐ 25 Stars",
        "support_stars_50": "⭐ 50 Stars",
        "support_stars_75": "⭐ 75 Stars",
        "support_stars_100": "⭐ 100 Stars",
        "support_invoice_title": "Support Zazyvala Tag Bot",
        "support_invoice_desc": "Donation of {amount} Telegram Stars ❤️",
        "support_invoice_label": "Project Support",
        "payment_success": "🎉 <b>Thank you so much for your support!</b>\n\nReceived {stars} Telegram Stars ❤️",
        "payment_error": "Payment could not be processed."
    },
    "ru": {
        "welcome": (
            "👋 *Добро пожаловать! Я бот для простого упоминания участников группы.*\n\n"
            "Как мной пользоваться:\n"
            "• Напишите `/калл` (или `калл`), чтобы упомянуть всех (со случайным или личным эмодзи).\n"
            "• Напишите `/калл ваше сообщение`, чтобы упомянуть всех вместе с вашим сообщением.\n"
            "• Используйте `/all` для общего упоминания.\n"
            "• Используйте `/сетми <эмодзи>`, чтобы закрепить за собой личный эмодзи!\n"
            "• Используйте `/ми`, чтобы проверить установленный эмодзи!\n"
            "• Используйте `/анрег` или напишите `анрег`, чтобы временно исключить себя из упоминаний.\n"
            "• Используйте `/language`, чтобы выбрать язык.\n\n"
            "Добавьте меня в группу, убедитесь, что все отправили хотя бы одно сообщение, чтобы я мог их запомнить, и готово!"
        ),
        "group_only": "This command can only be used in groups. / Эту команду можно использовать только в группах.",
        "lang_not_set": "Пожалуйста, сначала выберите язык с помощью команды /language. / Please choose a language first using /language.",
        "unreg_success": "Вы были исключены из упоминаний. (Вы будете добавлены снова, если отправите любое сообщение).",
        "cooldown": "Подождите {remaining} секунд перед следующей попыткой.",
        "no_active_users": "Я пока никого не видел. Участники должны отправить сообщения, чтобы я мог их упомянуть!",
        "setme_usage": "Пожалуйста, укажите эмодзи. Пример: /setme <эмодзи>",
        "invalid_emoji": "Это не похоже на допустимый эмодзи.",
        "emoji_taken": "Извините, этот эмодзи уже занят.",
        "setme_success": "Готово! Теперь ваш эмодзи: {emoji}",
        "me_success": "Ваш установленный эмодзи: {emoji}",
        "me_none": "У вас не установлен эмодзи. Используйте /setme <эмодзи>, чтобы установить его.",
        "lang_choose": "Пожалуйста, выберите язык:",
        "lang_success": "Язык успешно изменен на Русский! 🇷🇺",
        "rate_limit_stop": "⚠️ Слишком много сообщений. Лимит Telegram превышен, вызов приостановлен. Попробуйте позже.",
        "support_btn": "❤️ Поддержать Zazyvala",
        "support_text": (
            "❤️ <b>Поддержать проект</b>\n\n"
            'Этот бот бесплатный, с <a href="https://github.com/ryam1i/zazyvalatagbot">открытым исходным кодом</a>, без рекламы и телеметрии. '
            "Он разрабатывается и поддерживается исключительно на энтузиазме.\n\n"
            "Если вы хотите поддержать оплату серверов и дальнейшую разработку, "
            "вы можете отправить Telegram Stars ⭐ ниже:\n\n"
            "<i>Хотите отправить больше? Просто ответьте на это сообщение любым количеством Звёзд (например, 150 или 500).</i>"
        ),
        "support_stars_25": "⭐ 25 Звёзд",
        "support_stars_50": "⭐ 50 Звёзд",
        "support_stars_75": "⭐ 75 Звёзд",
        "support_stars_100": "⭐ 100 Звёзд",
        "support_invoice_title": "Поддержка Zazyvala Tag Bot",
        "support_invoice_desc": "Донат в размере {amount} Telegram Stars ❤️",
        "support_invoice_label": "Поддержка проекта",
        "payment_success": "🎉 <b>Большое спасибо за вашу поддержку!</b>\n\nПолучено {stars} Telegram Stars ❤️",
        "payment_error": "Не удалось обработать платёж."
    }
}

BOT_COMMANDS = {
    "en": [
        BotCommand("start", "Show welcome guide"),
        BotCommand("all", "Tag everyone in the group"),
        BotCommand("me", "Check your installed emoji"),
        BotCommand("setme", "Claim a personal emoji"),
        BotCommand("language", "Choose your language"),
        BotCommand("support", "Support the project with Stars"),
        BotCommand("unreg", "Exclude yourself from tagging"),
    ],
    "ru": [
        BotCommand("start", "Показать руководство"),
        BotCommand("all", "Упомянуть всех в группе"),
        BotCommand("me", "Проверить выбранный эмодзи"),
        BotCommand("setme", "Закрепить личный эмодзи"),
        BotCommand("language", "Выбрать язык"),
        BotCommand("support", "Поддержать проект Звёздами"),
        BotCommand("unreg", "Исключить себя из упоминаний"),
    ]
}

def group_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_chat or not update.effective_message or not update.effective_user:
            return
        if update.effective_chat.type not in ['group', 'supergroup']:
            await update.effective_message.reply_text(TRANSLATIONS["en"]["group_only"])
            return
        lang = get_cached_chat_language(update.effective_chat.id, context)
        if not lang:
            await update.effective_message.reply_text(TRANSLATIONS["en"]["lang_not_set"])
            return
        return await func(update, context, lang, *args, **kwargs)
    return wrapper

def user_cooldown(seconds: int = 3):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = update.effective_user.id if update.effective_user else None
            if not user_id or is_admin(user_id):
                return await func(update, context, *args, **kwargs)
            now = time.time()
            if now - USER_COOLDOWNS.get(user_id, 0) < seconds:
                return
            USER_COOLDOWNS[user_id] = now
            if len(USER_COOLDOWNS) > 500:
                expired = [uid for uid, t in USER_COOLDOWNS.items() if now - t > seconds]
                for uid in expired:
                    del USER_COOLDOWNS[uid]
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator

def enforce_cooldown(context: ContextTypes.DEFAULT_TYPE, user_id: Optional[int] = None) -> tuple[bool, int]:
    if user_id and is_admin(user_id):
        return True, 0
    now = time.time()
    last_call = context.chat_data.get("last_call", 0)
    time_passed = now - last_call
    if time_passed < COOLDOWN_SECONDS:
        return False, int(COOLDOWN_SECONDS - time_passed)
    context.chat_data["last_call"] = now
    return True, 0

async def send_user_mentions(update: Update, chat_id: int, lang: str, prefix_text: Optional[str] = None) -> None:
    if not update.effective_message:
        return

    batches = await run_db(get_user_batches, chat_id, 5)
    if not batches:
        await update.effective_message.reply_text(TRANSLATIONS[lang]["no_active_users"])
        return

    support_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(TRANSLATIONS[lang]["support_btn"], callback_data="open_support")]
    ])

    for idx, batch in enumerate(batches):
        is_last = (idx == len(batches) - 1)
        mentions = [
            f'<a href="tg://user?id={u.user_id}">{html.escape(u.emoji if u.emoji else random.choice(ALL_EMOJIS))}</a>'
            for u in batch
        ]
        text = " ".join(mentions)
        if prefix_text:
            safe_prefix = html.escape(prefix_text[:1000])
            text = f"{safe_prefix}\n\n{text}"

        reply_markup = support_markup if is_last else None

        for _ in range(3):
            try:
                await update.effective_message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
                break
            except RetryAfter as e:
                logger.warning(f"Rate limited in chat {chat_id}. Waiting {e.retry_after}s...")
                if e.retry_after > 10:
                    try:
                        await update.effective_message.reply_text(TRANSLATIONS[lang]["rate_limit_stop"])
                    except Exception:
                        pass
                    return
                await asyncio.sleep(e.retry_after + 0.5)
            except Exception as ex:
                logger.error(f"Failed to send mentions in chat {chat_id}: {ex}")
                break

        await asyncio.sleep(0.5)



async def track_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat or update.effective_user.is_bot:
        return

    if update.effective_chat.type not in ['group', 'supergroup']:
        return

    chat_title = update.effective_chat.title
    if chat_title and context.chat_data.get("title") != chat_title:
        await run_db(update_chat_title, update.effective_chat.id, chat_title, update.effective_chat.type)
        context.chat_data["title"] = chat_title

    if not get_cached_chat_language(update.effective_chat.id, context):
        return

    text = update.effective_message.text or ""
    if re.match(r'(?i)^/?(unreg|анрег)(@\w+)?(\s+.*)?$', text.strip()):
        return

    cache_key = (update.effective_chat.id, update.effective_user.id)
    now = time.time()
    if now - TRACKED_USERS_CACHE.get(cache_key, 0) < 600:
        return
    TRACKED_USERS_CACHE[cache_key] = now
    if len(TRACKED_USERS_CACHE) > 5000:
        expired = [k for k, t in TRACKED_USERS_CACHE.items() if now - t > 600]
        for k in expired:
            del TRACKED_USERS_CACHE[k]

    await run_db(
        upsert_user,
        update.effective_chat.id,
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.first_name
    )

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    chat_id = update.effective_chat.id
    lang = get_cached_chat_language(chat_id, context)

    if not lang:
        keyboard = [
            [
                InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
                InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        welcome_text = (
            "👋 *Welcome! / Добро пожаловать!*\n\n"
            "Please choose a language to initialize the bot:\n"
            "Пожалуйста, выберите язык для инициализации бота:"
        )
        await update.effective_message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.effective_message.reply_text(TRANSLATIONS[lang]["welcome"], parse_mode='Markdown')

@group_only
@user_cooldown(3)
async def unreg_command(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    await run_db(remove_user, update.effective_chat.id, update.effective_user.id)
    await update.effective_message.reply_text(TRANSLATIONS[lang]["unreg_success"])

@group_only
@user_cooldown(3)
async def setme_command(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    await run_db(upsert_user, chat_id, user_id, update.effective_user.username, update.effective_user.first_name)

    parts = (update.effective_message.text or "").strip().split()
    chosen_emoji = parts[1] if len(parts) > 1 else None

    if not chosen_emoji:
        await update.effective_message.reply_text(TRANSLATIONS[lang]["setme_usage"])
        return

    if not emoji.is_emoji(chosen_emoji):
        await update.effective_message.reply_text(TRANSLATIONS[lang]["invalid_emoji"])
        return

    if await run_db(is_emoji_taken_by_others, chat_id, user_id, chosen_emoji):
        await update.effective_message.reply_text(TRANSLATIONS[lang]["emoji_taken"])
        return

    await run_db(set_user_emoji, chat_id, user_id, chosen_emoji)
    await update.effective_message.reply_text(TRANSLATIONS[lang]["setme_success"].format(emoji=chosen_emoji))

@group_only
@user_cooldown(3)
async def me_command(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    def get_emoji():
        with db_cursor() as cur:
            res = cur.execute('SELECT emoji FROM users WHERE chat_id = ? AND user_id = ?', (chat_id, user_id)).fetchone()
            return res[0] if res else None

    emoji_str = await run_db(get_emoji)
    if emoji_str:
        await update.effective_message.reply_text(TRANSLATIONS[lang]["me_success"].format(emoji=emoji_str))
    else:
        await update.effective_message.reply_text(TRANSLATIONS[lang]["me_none"])

@group_only
async def all_command(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    allowed, remaining = enforce_cooldown(context, user_id)
    if not allowed:
        await update.effective_message.reply_text(TRANSLATIONS[lang]["cooldown"].format(remaining=remaining))
        return

    await send_user_mentions(update, update.effective_chat.id, lang)

@group_only
async def call_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    allowed, remaining = enforce_cooldown(context, user_id)
    if not allowed:
        await update.effective_message.reply_text(TRANSLATIONS[lang]["cooldown"].format(remaining=remaining))
        return

    text = update.effective_message.text or ""
    clean_text = re.sub(r'(?i)^/?(call|калл)(@\w+)?\b\s*', '', text).strip()
    if len(clean_text) > 1000:
        clean_text = clean_text[:1000]

    prefix = clean_text if clean_text else None
    await send_user_mentions(update, update.effective_chat.id, lang, prefix_text=prefix)

async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    chat_id = update.effective_chat.id
    lang = get_cached_chat_language(chat_id, context) or "en"

    keyboard = [
        [
            InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
            InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.effective_message.reply_text(TRANSLATIONS[lang]["lang_choose"], reply_markup=reply_markup)

async def send_star_invoice(chat_id: int, amount: int, context: ContextTypes.DEFAULT_TYPE, lang: str = "ru") -> None:
    prices = [LabeledPrice(label=TRANSLATIONS[lang]["support_invoice_label"], amount=amount)]
    await context.bot.send_invoice(
        chat_id=chat_id,
        title=TRANSLATIONS[lang]["support_invoice_title"],
        description=TRANSLATIONS[lang]["support_invoice_desc"].format(amount=amount),
        payload=f"support_stars_{amount}",
        currency="XTR",
        prices=prices,
        provider_token=""
    )

async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return

    lang = get_user_or_chat_lang(update, context)

    keyboard = [
        [
            InlineKeyboardButton(TRANSLATIONS[lang]["support_stars_25"], callback_data="donate_stars_25"),
            InlineKeyboardButton(TRANSLATIONS[lang]["support_stars_50"], callback_data="donate_stars_50"),
        ],
        [
            InlineKeyboardButton(TRANSLATIONS[lang]["support_stars_75"], callback_data="donate_stars_75"),
            InlineKeyboardButton(TRANSLATIONS[lang]["support_stars_100"], callback_data="donate_stars_100"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.effective_message.reply_text(
        TRANSLATIONS[lang]["support_text"],
        reply_markup=reply_markup,
        parse_mode='HTML',
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

async def custom_stars_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_message.reply_to_message:
        return
    replied = update.effective_message.reply_to_message
    if not replied.from_user or replied.from_user.id != context.bot.id:
        return
    replied_text = replied.text or replied.caption or ""
    if not any(header in replied_text for header in ["Support the Project", "Поддержать проект"]):
        return

    text = (update.effective_message.text or "").strip()
    if len(text) > 5:
        return
    if text.isdigit():
        amount = int(text)
        if 100 < amount <= 25000:
            lang = get_user_or_chat_lang(update, context)
            await send_star_invoice(update.effective_chat.id, amount, context, lang=lang)

async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if not query:
        return
    if query.invoice_payload.startswith("support_stars_"):
        await query.answer(ok=True)
    else:
        lang = get_user_or_chat_lang(update, context)
        await query.answer(ok=False, error_message=TRANSLATIONS[lang]["payment_error"])

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_message.successful_payment or not update.effective_chat:
        return
    stars = update.effective_message.successful_payment.total_amount
    chat_id = update.effective_chat.id
    lang = get_user_or_chat_lang(update, context)
    await update.effective_message.reply_text(
        TRANSLATIONS[lang]["payment_success"].format(stars=stars),
        parse_mode="HTML"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.message:
        return

    if query.data == "open_support":
        await query.answer()
        await support_command(update, context)
        return

    if query.data.startswith("donate_stars_"):
        await query.answer()
        user_id = query.from_user.id
        now = time.time()

        if now - BUTTON_COOLDOWNS.get(user_id, 0) < 10:
            return
        BUTTON_COOLDOWNS[user_id] = now

        if len(BUTTON_COOLDOWNS) > 500:
            expired = [uid for uid, t in BUTTON_COOLDOWNS.items() if now - t > 10]
            for uid in expired:
                del BUTTON_COOLDOWNS[uid]
        amount = int(query.data.split("_")[-1])
        chat_id = query.message.chat.id
        lang = get_user_or_chat_lang(update, context)
        try:
            await send_star_invoice(query.message.chat.id, amount, context, lang=lang)
        except RetryAfter as e:
            logger.warning(f"Rate limited on invoice: {e.retry_after}s")
        except Exception as e:
            logger.error(f"Failed to send invoice: {e}")
        return

    if not query.data.startswith("lang_"):
        return

    chat_id = query.message.chat.id
    user_id = query.from_user.id
    lang_code = query.data.split("_")[1]

    if query.message.chat.type in ['group', 'supergroup']:
        try:
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            is_group_admin = chat_member.status in ['administrator', 'creator']
        except Exception as e:
            logger.error(f"Error checking admin status: {e}")
            is_group_admin = False

        if not is_group_admin:
            await query.answer(
                text="Only group administrators can choose the language. / Только администраторы группы могут выбирать язык.",
                show_alert=True
            )
            return

    await query.answer()
    await run_db(set_chat_language, chat_id, lang_code, query.message.chat.title, query.message.chat.type)
    context.chat_data["lang"] = lang_code

    try:
        commands_to_set = BOT_COMMANDS.get(lang_code, BOT_COMMANDS["ru"])
        await context.bot.set_my_commands(commands_to_set, scope=BotCommandScopeChat(chat_id=chat_id))
    except Exception as e:
        logger.error(f"Failed to set scoped bot commands for chat {chat_id}: {e}")

    welcome_text = f"{TRANSLATIONS[lang_code]['lang_success']}\n\n{TRANSLATIONS[lang_code]['welcome']}"
    await query.edit_message_text(welcome_text, parse_mode='Markdown')

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return

    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    backup_file = await run_db(backup_db)
    if not backup_file or not os.path.exists(backup_file):
        await update.effective_message.reply_text("❌ Error creating database backup.")
        return

    file_size_kb = os.path.getsize(backup_file) / 1024
    backup_filename = os.path.basename(backup_file)
    success_msg = (
        f"✅ <b>Database backup created successfully!</b>\n\n"
        f"• <b>File:</b> <code>{html.escape(backup_filename)}</code>\n"
        f"• <b>Size:</b> <code>{file_size_kb:.1f} KB</code>"
    )

    if update.effective_chat and update.effective_chat.type == "private":
        await update.effective_message.reply_text(success_msg, parse_mode="HTML")
        try:
            with open(backup_file, "rb") as doc:
                await context.bot.send_document(
                    chat_id=user_id,
                    document=doc,
                    filename=backup_filename,
                    caption="📦 Live backup snapshot of users.db"
                )
        except Exception as e:
            logger.error(f"Failed to send backup document to admin: {e}")
    else:
        await update.effective_message.reply_text(success_msg, parse_mode="HTML")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return

    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    replied_msg = update.effective_message.reply_to_message
    if not replied_msg:
        return

    if context.args and len(context.args) > 0:
        try:
            target_chats = [int(context.args[0])]
        except ValueError:
            return
    else:
        target_chats = await run_db(get_all_group_chats)

    if not target_chats:
        return

    status_msg = await update.effective_message.reply_text(
        f"📢 <b>Starting broadcast...</b> (targets: <code>{len(target_chats)}</code>)",
        parse_mode='HTML'
    )

    success_count = 0
    fail_count = 0
    removed_count = 0

    from_chat_id = update.effective_chat.id
    message_to_copy_id = replied_msg.message_id

    for chat_id in target_chats:
        sent = False
        chat_removed = False
        for _ in range(3):
            try:
                await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=from_chat_id,
                    message_id=message_to_copy_id
                )
                sent = True
                break
            except RetryAfter as e:
                logger.warning(f"Rate limited during broadcast in chat {chat_id}. Waiting {e.retry_after}s...")
                await asyncio.sleep(e.retry_after)
            except Forbidden as e:
                logger.warning(f"Bot was kicked/blocked in chat {chat_id}: {e}. Removing chat from database.")
                await run_db(delete_chat, chat_id)
                chat_removed = True
                break
            except BadRequest as e:
                err_msg = str(e).lower()
                if any(x in err_msg for x in ["chat not found", "chat was deactivated", "chat was deleted", "have no rights"]):
                    logger.warning(f"Chat {chat_id} is no longer accessible ({e}). Removing from database.")
                    await run_db(delete_chat, chat_id)
                    chat_removed = True
                else:
                    logger.error(f"BadRequest when broadcasting to chat {chat_id}: {e}")
                break
            except Exception as e:
                logger.error(f"Failed to broadcast to chat {chat_id}: {e}")
                break

        if sent:
            success_count += 1
        else:
            fail_count += 1
            if chat_removed:
                removed_count += 1

        await asyncio.sleep(0.1)

    report_text = (
        f"<b>Broadcast completed!</b>\n\n"
        f"<b>Successfully sent:</b> <code>{success_count}</code>\n"
        f"<b>Failed:</b> <code>{fail_count}</code>\n"
        f"<b>Removed unreachable groups:</b> <code>{removed_count}</code>\n"
        f"<b>Total targets:</b> <code>{len(target_chats)}</code>"
    )
    await status_msg.edit_text(report_text, parse_mode='HTML')

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.my_chat_member:
        return

    chat = update.my_chat_member.chat
    chat_id = chat.id
    old_status = update.my_chat_member.old_chat_member.status
    new_status = update.my_chat_member.new_chat_member.status

    logger.info(f"Bot status updated in chat {chat_id}: {old_status} -> {new_status}")

    if new_status in [ChatMemberStatus.BANNED, ChatMemberStatus.LEFT]:
        await run_db(delete_chat, chat_id)
        context.chat_data.clear()
        logger.info(f"Removed chat {chat_id} from database.")
        return

    if old_status in [ChatMemberStatus.BANNED, ChatMemberStatus.LEFT] and new_status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR]:
        if chat.type in ['group', 'supergroup']:
            if chat.title:
                await run_db(update_chat_title, chat_id, chat.title, chat.type)

            if not get_cached_chat_language(chat_id, context):
                keyboard = [
                    [
                        InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
                        InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                welcome_text = (
                    "👋 *Welcome! / Добро пожаловать!*\n\n"
                    "Please choose a language to initialize the bot:\n"
                    "Пожалуйста, выберите язык для инициализации бота:"
                )
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=welcome_text,
                        reply_markup=reply_markup,
                        parse_mode='Markdown'
                    )
                except Exception as e:
                    logger.error(f"Failed to send welcome message in chat {chat_id}: {e}")

async def on_chat_member_updated(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.chat_member:
        return
    chat_id = update.chat_member.chat.id
    user = update.chat_member.new_chat_member.user
    if not user or user.is_bot:
        return

    new_status = update.chat_member.new_chat_member.status
    if new_status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
        await run_db(delete_user, chat_id, user.id)

async def on_chat_migration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.migrate_to_chat_id:
        return
    old_chat_id = message.chat_id
    new_chat_id = message.migrate_to_chat_id
    logger.info(f"Chat migrated: {old_chat_id} -> {new_chat_id}")
    await run_db(migrate_chat_id, old_chat_id, new_chat_id)
    if old_chat_id in context.application.chat_data:
        context.application.chat_data[new_chat_id] = context.application.chat_data.pop(old_chat_id)

async def periodic_wal_checkpoint() -> None:
    while True:
        await asyncio.sleep(3600)
        def do_checkpoint():
            with db_cursor() as cur:
                cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        try:
            await run_db(do_checkpoint)
        except Exception as e:
            logger.error(f"Error during periodic WAL checkpoint: {e}")

async def post_init(application: Application) -> None:
    asyncio.create_task(periodic_wal_checkpoint())
    try:
        scopes = [
            BotCommandScopeDefault(),
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllGroupChats(),
        ]
        for s in scopes:
            await application.bot.set_my_commands(BOT_COMMANDS["ru"], scope=s)
            await application.bot.set_my_commands(BOT_COMMANDS["en"], scope=s, language_code="en")
            await application.bot.set_my_commands(BOT_COMMANDS["ru"], scope=s, language_code="ru")
    except Exception as e:
        logger.warning(f"Could not set bot commands during startup ({e}). Continuing...")

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Please set the TELEGRAM_BOT_TOKEN environment variable in the .env file.")
        return

    init_db()

    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("language", language_command))
    application.add_handler(CommandHandler("support", support_command))
    application.add_handler(CommandHandler("backup", backup_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("all", all_command))

    application.add_handler(MessageHandler(filters.Regex(r'(?i)^/?(call|калл)(?:@\w+)?(?:\s|$)'), call_text_command))
    application.add_handler(MessageHandler(filters.Regex(r'(?i)^/?(unreg|анрег)(?:@\w+)?(?:\s|$)'), unreg_command))
    application.add_handler(MessageHandler(filters.Regex(r'(?i)^/?(setme|сетми)(?:@\w+)?(?:\s|$)'), setme_command))
    application.add_handler(MessageHandler(filters.Regex(r'(?i)^/?(me|ми)(?:@\w+)?$'), me_command))

    application.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    application.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, custom_stars_reply_handler))

    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(ChatMemberHandler(on_chat_member_updated, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_chat_migration))

    application.add_handler(MessageHandler(filters.ALL, track_users), group=1)

    logger.info("Zazyvala Tag Bot is starting up...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
