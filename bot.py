import os
import asyncio
import logging
import html

from aiohttp import web
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# SETTINGS
# =========================================================

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]

PUBLIC_URL = (
    os.environ.get("WEBHOOK_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
)

PORT = int(os.environ.get("PORT", "10000"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# =========================================================
# FAST CACHE
# =========================================================

ADMIN_CACHE = {ADMIN_ID}
USER_CACHE = set()
START_TEXT_CACHE = None

db_pool = None


# =========================================================
# DATABASE POOL
# =========================================================

def init_db_pool():
    global db_pool

    if db_pool is None:
        db_pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=1,
            max_size=5,
            kwargs={
                "row_factory": dict_row
            }
        )

        db_pool.wait()

        logger.info("PostgreSQL connection pool ready")


def query(sql, params=(), fetch=False, one=False):
    global db_pool

    if db_pool is None:
        init_db_pool()

    with db_pool.connection() as connection:
        with connection.cursor() as cursor:

            cursor.execute(
                sql,
                params
            )

            if fetch:
                rows = cursor.fetchall()

                if one:
                    return rows[0] if rows else None

                return rows

        connection.commit()

    return None


# =========================================================
# DATABASE TABLES
# =========================================================

def init_admin_table():

    # -----------------------------------------------------
    # ADMINS
    # -----------------------------------------------------

    query(
        """
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    query(
        """
        INSERT INTO admins (user_id)
        VALUES (%s)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (ADMIN_ID,)
    )

    # -----------------------------------------------------
    # MULTIPLE FILES FOR EACH BUTTON
    # -----------------------------------------------------

    query(
        """
        CREATE TABLE IF NOT EXISTS button_files (
            id BIGSERIAL PRIMARY KEY,
            button_id BIGINT NOT NULL,
            source_chat_id BIGINT NOT NULL,
            source_message_id BIGINT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            CONSTRAINT unique_button_file
            UNIQUE (
                button_id,
                source_chat_id,
                source_message_id
            )
        )
        """
    )

    # -----------------------------------------------------
    # MOVE OLD FILES INTO NEW TABLE
    # -----------------------------------------------------

    query(
        """
        INSERT INTO button_files (
            button_id,
            source_chat_id,
            source_message_id
        )
        SELECT
            id,
            source_chat_id,
            source_message_id
        FROM buttons
        WHERE kind = 'file'
        AND source_chat_id IS NOT NULL
        AND source_message_id IS NOT NULL
        ON CONFLICT (
            button_id,
            source_chat_id,
            source_message_id
        )
        DO NOTHING
        """
    )

    # -----------------------------------------------------
    # LOAD ADMINS
    # -----------------------------------------------------

    admins = query(
        """
        SELECT user_id
        FROM admins
        """,
        fetch=True
    )

    ADMIN_CACHE.clear()
    ADMIN_CACHE.add(ADMIN_ID)

    for admin in admins:
        ADMIN_CACHE.add(
            admin["user_id"]
        )

    logger.info(
        "Loaded %s admins",
        len(ADMIN_CACHE)
    )


# =========================================================
# ADMIN CHECK
# =========================================================

def is_main_admin(update):

    user = update.effective_user

    return (
        user is not None
        and user.id == ADMIN_ID
    )


def is_admin(update):

    user = update.effective_user

    if not user:
        return False

    return user.id in ADMIN_CACHE


# =========================================================
# KEYBOARDS
# =========================================================

def admin_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("➕ افزودن دکمه"),
                KeyboardButton("🛠 مدیریت دکمه‌ها")
            ],
            [
                KeyboardButton("✏️ متن /start"),
                KeyboardButton("📊 آمار کاربران")
            ],
            [
                KeyboardButton("📢 ارسال همگانی"),
                KeyboardButton("👥 مدیریت ادمین‌ها")
            ],
            [
                KeyboardButton("👤 منوی کاربر")
            ],
        ],
        resize_keyboard=True
    )


def admin_management_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("➕ افزودن ادمین"),
                KeyboardButton("🗑 حذف ادمین")
            ],
            [
                KeyboardButton("👥 لیست ادمین‌ها")
            ],
            [
                KeyboardButton("🔙 لغو / بازگشت")
            ]
        ],
        resize_keyboard=True
    )


def button_management_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("➕ افزودن دکمه اصلی"),
                KeyboardButton("➕ افزودن زیرمنو")
            ],
            [
                KeyboardButton("✏️ تغییر نام"),
                KeyboardButton("📁 تغییر فایل / پیام")
            ],
            [
                KeyboardButton("🗑 حذف دکمه")
            ],
            [
                KeyboardButton("🔙 لغو / بازگشت")
            ]
        ],
        resize_keyboard=True
    )


def back_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("🔙 لغو / بازگشت")
            ]
        ],
        resize_keyboard=True
    )


# =========================================================
# USER MENU
# =========================================================

def user_keyboard(parent_id=None, page=0):

    buttons = query(
        """
        SELECT id, title, kind
        FROM buttons
        WHERE parent_id IS NOT DISTINCT FROM %s
        ORDER BY sort_order, id
        """,
        (parent_id,),
        fetch=True
    )

    per_page = 8

    start = page * per_page

    current = buttons[
        start:start + per_page
    ]

    keyboard = []

    for button in current:

        keyboard.append(
            [
                KeyboardButton(
                    button["title"]
                )
            ]
        )

    navigation = []

    if page > 0:

        navigation.append(
            KeyboardButton(
                "⬅️ صفحه قبل"
            )
        )

    if start + per_page < len(buttons):

        navigation.append(
            KeyboardButton(
                "➡️ صفحه بعد"
            )
        )

    if navigation:

        keyboard.append(navigation)

    if parent_id is not None:

        keyboard.append(
            [
                KeyboardButton(
                    "🔙 بازگشت"
                )
            ]
        )

    return (
        ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        ),
        current,
        len(buttons)
    )


# =========================================================
# START TEXT CACHE
# =========================================================

def load_start_text():

    global START_TEXT_CACHE

    row = query(
        """
        SELECT value
        FROM settings
        WHERE key = 'start_text'
        """,
        fetch=True,
        one=True
    )

    if row:

        START_TEXT_CACHE = row["value"]

    else:

        START_TEXT_CACHE = (
            "سلام 👋 به ربات ما خوش آمدید!"
        )


def get_start_text():

    global START_TEXT_CACHE

    if START_TEXT_CACHE is None:

        load_start_text()

    return START_TEXT_CACHE


# =========================================================
# STATE
# =========================================================

def set_state(context, state, **data):

    context.user_data.clear()

    context.user_data["state"] = state

    for key, value in data.items():

        context.user_data[key] = value


# =========================================================
# SAVE USER
# =========================================================

async def save_user(update):

    user = update.effective_user

    if not user:
        return

    user_id = user.id

    if user_id in USER_CACHE:
        return

    try:

        query(
            """
            INSERT INTO users
            (
                user_id,
                first_name,
                username
            )
            VALUES
            (%s, %s, %s)

            ON CONFLICT (user_id)
            DO UPDATE SET
                first_name = EXCLUDED.first_name,
                username = EXCLUDED.username
            """,
            (
                user_id,
                user.first_name or "",
                user.username or ""
            )
        )

        USER_CACHE.add(user_id)

    except Exception as e:

        logger.error(
            "Save user error: %s",
            e
        )


# =========================================================
# START
# =========================================================

async def start(update, context):

    await save_user(update)

    context.user_data.clear()

    keyboard, _, _ = user_keyboard()

    await update.message.reply_text(
        get_start_text(),
        reply_markup=keyboard
    )


# =========================================================
# ADMIN COMMAND
# =========================================================

async def admin_command(update, context):

    if not is_admin(update):
        return

    context.user_data.clear()

    await update.message.reply_text(
        "⚙️ پنل مدیریت ربات",
        reply_markup=admin_keyboard()
    )


# =========================================================
# USER HOME
# =========================================================

async def show_root(update):

    keyboard, _, _ = user_keyboard()

    await update.message.reply_text(
        get_start_text(),
        reply_markup=keyboard
    )


# =========================================================
# CREATE BUTTON
# =========================================================

async def create_button(
    title,
    kind,
    parent_id=None,
    source_chat_id=None,
    source_message_id=None,
    value=None
):

    result = query(
        """
        SELECT COALESCE(MAX(sort_order), 0) AS max_sort
        FROM buttons
        WHERE parent_id IS NOT DISTINCT FROM %s
        """,
        (parent_id,),
        fetch=True,
        one=True
    )

    sort_order = result["max_sort"] + 1

    row = query(
        """
        INSERT INTO buttons
        (
            parent_id,
            title,
            kind,
            source_chat_id,
            source_message_id,
            value,
            sort_order
        )
        VALUES
        (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            parent_id,
            title,
            kind,
            source_chat_id,
            source_message_id,
            value,
            sort_order
        ),
        fetch=True,
        one=True
    )

    return row["id"]


# =========================================================
# ADD FILE TO EXISTING BUTTON
# =========================================================

async def add_file_to_button(
    button_id,
    source_chat_id,
    source_message_id
):

    query(
        """
        INSERT INTO button_files
        (
            button_id,
            source_chat_id,
            source_message_id
        )
        VALUES
        (%s, %s, %s)
        ON CONFLICT (
            button_id,
            source_chat_id,
            source_message_id
        )
        DO NOTHING
        """,
        (
            button_id,
            source_chat_id,
            source_message_id
        )
    )


# =========================================================
# ADD BUTTON
# =========================================================

async def add_button_start(update, context):

    if not is_admin(update):
        return

    set_state(
        context,
        "add_name",
        parent_id=None,
        admin_section="button_management"
    )

    await update.message.reply_text(
        "➕ نام دکمه را بفرست:",
        reply_markup=back_keyboard()
    )


# =========================================================
# BUTTON TYPE
# =========================================================

async def ask_button_type(update, context):

    keyboard = ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("📁 فایل / پیام"),
                KeyboardButton("📂 منوی فرعی")
            ],
            [
                KeyboardButton("📝 متن"),
                KeyboardButton("🔗 لینک")
            ],
            [
                KeyboardButton("🔙 لغو / بازگشت")
            ]
        ],
        resize_keyboard=True
    )

    await update.message.reply_text(
        "نوع دکمه را انتخاب کن:",
        reply_markup=keyboard
    )


# =========================================================
# ADD CHILD MENU
# =========================================================

async def add_child_start(update, context):

    menus = query(
        """
        SELECT id, title
        FROM buttons
        WHERE kind = 'menu'
        ORDER BY id
        """,
        fetch=True
    )

    if not menus:

        await update.message.reply_text(
            "❌ هنوز هیچ منوی فرعی ساخته نشده است.",
            reply_markup=button_management_keyboard()
        )

        return

    keyboard = []

    for menu in menus:

        keyboard.append(
            [
                KeyboardButton(
                    menu["title"]
                )
            ]
        )

    keyboard.append(
        [
            KeyboardButton(
                "🔙 لغو / بازگشت"
            )
        ]
    )

    set_state(
        context,
        "child_parent",
        admin_section="button_management"
    )

    await update.message.reply_text(
        "📂 منوی والد را انتخاب کن:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
    )


# =========================================================
# MANAGEMENT
# =========================================================

async def management_menu(update, context):

    context.user_data.clear()

    context.user_data["admin_section"] = (
        "button_management"
    )

    await update.message.reply_text(
        "🛠 مدیریت دکمه‌ها:",
        reply_markup=button_management_keyboard()
    )


# =========================================================
# RENAME
# =========================================================

async def rename_start(update, context):

    buttons = query(
        """
        SELECT id, title
        FROM buttons
        ORDER BY id
        """,
        fetch=True
    )

    if not buttons:

        await update.message.reply_text(
            "❌ هنوز دکمه‌ای ساخته نشده.",
            reply_markup=button_management_keyboard()
        )

        return

    keyboard = [
        [
            KeyboardButton(
                button["title"]
            )
        ]
        for button in buttons
    ]

    keyboard.append(
        [
            KeyboardButton(
                "🔙 لغو / بازگشت"
            )
        ]
    )

    set_state(
        context,
        "rename_choose",
        admin_section="button_management"
    )

    await update.message.reply_text(
        "✏️ دکمه‌ای که می‌خواهی تغییر نام بده انتخاب کن:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
    )


# =========================================================
# DELETE
# =========================================================

async def delete_start(update, context):

    buttons = query(
        """
        SELECT id, title
        FROM buttons
        ORDER BY id
        """,
        fetch=True
    )

    if not buttons:

        await update.message.reply_text(
            "❌ هنوز دکمه‌ای ساخته نشده.",
            reply_markup=button_management_keyboard()
        )

        return

    keyboard = [
        [
            KeyboardButton(
                button["title"]
            )
        ]
        for button in buttons
    ]

    keyboard.append(
        [
            KeyboardButton(
                "🔙 لغو / بازگشت"
            )
        ]
    )

    set_state(
        context,
        "delete_choose",
        admin_section="button_management"
    )

    await update.message.reply_text(
        "🗑 دکمه‌ای که می‌خواهی حذف کنی انتخاب کن:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
    )


# =========================================================
# ADD MORE FILE / MESSAGE
# =========================================================

async def change_file_start(update, context):

    buttons = query(
        """
        SELECT id, title
        FROM buttons
        WHERE kind = 'file'
        ORDER BY id
        """,
        fetch=True
    )

    if not buttons:

        await update.message.reply_text(
            "❌ هنوز هیچ دکمه فایل / پیام ساخته نشده است.",
            reply_markup=button_management_keyboard()
        )

        return

    keyboard = []

    for button in buttons:

        keyboard.append(
            [
                KeyboardButton(
                    button["title"]
                )
            ]
        )

    keyboard.append(
        [
            KeyboardButton(
                "🔙 لغو / بازگشت"
            )
        ]
    )

    set_state(
        context,
        "change_file_choose",
        admin_section="button_management"
    )

    await update.message.reply_text(
        "📁 دکمه‌ای را انتخاب کن تا فایل یا پیام جدید به آن اضافه شود:\n\n"
        "⚠️ فایل‌های قبلی حذف یا جایگزین نمی‌شوند.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
    )


# =========================================================
# START TEXT EDIT
# =========================================================

async def edit_start_text(update, context):

    set_state(
        context,
        "start_text",
        admin_section="main"
    )

    await update.message.reply_text(
        "✏️ متن فعلی:\n\n"
        + get_start_text()
        + "\n\n"
        "متن جدید را بفرست:",
        reply_markup=back_keyboard()
    )


# =========================================================
# STATISTICS
# =========================================================

async def statistics(update, context):

    result = query(
        """
        SELECT COUNT(*) AS total
        FROM users
        """,
        fetch=True,
        one=True
    )

    await update.message.reply_text(
        f"📊 تعداد کاربران: {result['total']}",
        reply_markup=admin_keyboard()
    )


# =========================================================
# BROADCAST
# =========================================================

async def broadcast_start(update, context):

    set_state(
        context,
        "broadcast",
        admin_section="main"
    )

    await update.message.reply_text(
        "📢 حالا هر چیزی که می‌خواهی برای کاربران ارسال شود بفرست.\n\n"
        "متن، عکس، ویدیو، PDF، فایل، صوت و غیره.",
        reply_markup=back_keyboard()
    )


# =========================================================
# ADMIN MANAGEMENT
# =========================================================

async def admin_management(update, context):

    if not is_main_admin(update):

        await update.message.reply_text(
            "❌ فقط ادمین اصلی می‌تواند ادمین‌ها را مدیریت کند."
        )

        return

    context.user_data.clear()

    context.user_data["admin_section"] = (
        "admin_management"
    )

    await update.message.reply_text(
        "👥 مدیریت ادمین‌ها\n\n"
        "از گزینه‌های زیر استفاده کن:",
        reply_markup=admin_management_keyboard()
    )


async def add_admin_start(update, context):

    if not is_main_admin(update):
        return

    set_state(
        context,
        "add_admin",
        admin_section="admin_management"
    )

    await update.message.reply_text(
        "➕ آیدی عددی کاربر را بفرست.\n\n"
        "مثال:\n"
        "123456789",
        reply_markup=back_keyboard()
    )


async def remove_admin_start(update, context):

    if not is_main_admin(update):
        return

    admins = query(
        """
        SELECT user_id
        FROM admins
        ORDER BY added_at
        """,
        fetch=True
    )

    if not admins:

        await update.message.reply_text(
            "❌ هیچ ادمینی وجود ندارد.",
            reply_markup=admin_management_keyboard()
        )

        return

    keyboard = []

    for admin in admins:

        user_id = admin["user_id"]

        if user_id == ADMIN_ID:

            title = (
                f"👑 {user_id} (ادمین اصلی)"
            )

        else:

            title = f"👤 {user_id}"

        keyboard.append(
            [
                KeyboardButton(title)
            ]
        )

    keyboard.append(
        [
            KeyboardButton(
                "🔙 لغو / بازگشت"
            )
        ]
    )

    set_state(
        context,
        "remove_admin",
        admin_section="admin_management"
    )

    await update.message.reply_text(
        "🗑 ادمینی که می‌خواهی حذف کنی انتخاب کن:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
    )


async def admin_list(update, context):

    if not is_main_admin(update):
        return

    admins = query(
        """
        SELECT user_id, added_at
        FROM admins
        ORDER BY added_at
        """,
        fetch=True
    )

    if not admins:

        await update.message.reply_text(
            "❌ هیچ ادمینی ثبت نشده.",
            reply_markup=admin_management_keyboard()
        )

        return

    text = "👥 لیست ادمین‌ها:\n\n"

    for index, admin in enumerate(
        admins,
        1
    ):

        user_id = admin["user_id"]

        if user_id == ADMIN_ID:

            text += (
                f"{index}. 👑 {user_id} — ادمین اصلی\n"
            )

        else:

            text += (
                f"{index}. 👤 {user_id}\n"
            )

    await update.message.reply_text(
        text,
        reply_markup=admin_management_keyboard()
    )


# =========================================================
# ADMIN STATE
# =========================================================

async def handle_admin_state(update, context):

    if not is_admin(update):
        return False

    message = update.message

    if not message:
        return False

    text = message.text or ""

    state = context.user_data.get(
        "state"
    )

    admin_section = context.user_data.get(
        "admin_section",
        "main"
    )

    # =====================================================
    # CANCEL / BACK
    # =====================================================

    if text == "🔙 لغو / بازگشت":

        # -----------------------------------------------
        # اگر داخل یک عملیات هستیم
        # -----------------------------------------------

        if state:

            context.user_data.clear()

            if admin_section == "button_management":

                context.user_data["admin_section"] = (
                    "button_management"
                )

                await message.reply_text(
                    "🛠 مدیریت دکمه‌ها:",
                    reply_markup=button_management_keyboard()
                )

                return True

            if admin_section == "admin_management":

                context.user_data["admin_section"] = (
                    "admin_management"
                )

                await message.reply_text(
                    "👥 مدیریت ادمین‌ها:",
                    reply_markup=admin_management_keyboard()
                )

                return True

            await message.reply_text(
                "⚙️ پنل مدیریت",
                reply_markup=admin_keyboard()
            )

            return True

        # -----------------------------------------------
        # اگر خود صفحه مدیریت دکمه‌ها هستیم
        # -----------------------------------------------

        if admin_section == "button_management":

            context.user_data.clear()

            await message.reply_text(
                "⚙️ پنل مدیریت",
                reply_markup=admin_keyboard()
            )

            return True

        # -----------------------------------------------
        # اگر صفحه مدیریت ادمین‌ها هستیم
        # -----------------------------------------------

        if admin_section == "admin_management":

            context.user_data.clear()

            await message.reply_text(
                "⚙️ پنل مدیریت",
                reply_markup=admin_keyboard()
            )

            return True

        # -----------------------------------------------
        # حالت عادی
        # -----------------------------------------------

        context.user_data.clear()

        await message.reply_text(
            "⚙️ پنل مدیریت",
            reply_markup=admin_keyboard()
        )

        return True

    # =====================================================
    # ADD ADMIN
    # =====================================================

    if state == "add_admin":

        if not is_main_admin(update):

            context.user_data.clear()

            await message.reply_text(
                "❌ فقط ادمین اصلی می‌تواند ادمین اضافه کند.",
                reply_markup=admin_keyboard()
            )

            return True

        try:

            new_admin_id = int(
                text.strip()
            )

        except ValueError:

            await message.reply_text(
                "❌ آیدی باید فقط عدد باشد."
            )

            return True

        if new_admin_id <= 0:

            await message.reply_text(
                "❌ آیدی نامعتبر است."
            )

            return True

        if new_admin_id in ADMIN_CACHE:

            context.user_data.clear()

            await message.reply_text(
                "⚠️ این کاربر قبلاً ادمین است.",
                reply_markup=admin_management_keyboard()
            )

            return True

        query(
            """
            INSERT INTO admins (user_id)
            VALUES (%s)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (new_admin_id,)
        )

        ADMIN_CACHE.add(
            new_admin_id
        )

        context.user_data.clear()

        await message.reply_text(
            "✅ ادمین با موفقیت اضافه شد.\n\n"
            f"🆔 ID: {new_admin_id}\n\n"
            "این کاربر اکنون می‌تواند /admin را بزند.",
            reply_markup=admin_management_keyboard()
        )

        return True

    # =====================================================
    # REMOVE ADMIN
    # =====================================================

    if state == "remove_admin":

        if not is_main_admin(update):

            context.user_data.clear()

            await message.reply_text(
                "❌ فقط ادمین اصلی می‌تواند ادمین حذف کند.",
                reply_markup=admin_keyboard()
            )

            return True

        try:

            if text.startswith("👑"):

                admin_id = int(
                    text.split(
                        "👑",
                        1
                    )[1].split(
                        "(",
                        1
                    )[0].strip()
                )

            elif text.startswith("👤"):

                admin_id = int(
                    text.split(
                        "👤",
                        1
                    )[1].strip()
                )

            else:

                raise ValueError

        except Exception:

            await message.reply_text(
                "❌ ادمین انتخاب‌شده معتبر نیست."
            )

            return True

        if admin_id == ADMIN_ID:

            context.user_data.clear()

            await message.reply_text(
                "❌ ادمین اصلی قابل حذف نیست. 👑",
                reply_markup=admin_management_keyboard()
            )

            return True

        query(
            """
            DELETE FROM admins
            WHERE user_id = %s
            """,
            (admin_id,)
        )

        ADMIN_CACHE.discard(
            admin_id
        )

        context.user_data.clear()

        await message.reply_text(
            "✅ ادمین حذف شد.\n\n"
            f"🆔 ID: {admin_id}",
            reply_markup=admin_management_keyboard()
        )

        return True

    # =====================================================
    # BUTTON NAME
    # =====================================================

    if state == "add_name":

        if not text.strip():

            await message.reply_text(
                "❌ نام دکمه نمی‌تواند خالی باشد."
            )

            return True

        context.user_data["title"] = (
            text.strip()
        )

        context.user_data["state"] = (
            "add_kind"
        )

        await ask_button_type(
            update,
            context
        )

        return True

    # =====================================================
    # BUTTON TYPE
    # =====================================================

    if state == "add_kind":

        title = context.user_data[
            "title"
        ]

        parent_id = context.user_data.get(
            "parent_id"
        )

        # -------------------------------------------------
        # SUB MENU
        # -------------------------------------------------

        if text == "📂 منوی فرعی":

            await create_button(
                title=title,
                kind="menu",
                parent_id=parent_id
            )

            context.user_data.clear()

            await message.reply_text(
                "✅ منوی فرعی ساخته شد.",
                reply_markup=admin_keyboard()
            )

            return True

        # -------------------------------------------------
        # FILE
        # -------------------------------------------------

        if text == "📁 فایل / پیام":

            context.user_data["state"] = (
                "add_file"
            )

            await message.reply_text(
                "📁 حالا اولین فایل یا پیام این دکمه را بفرست.\n\n"
                "بعداً هر تعداد فایل دیگری هم خواستی "
                "می‌توانی با گزینه «📁 تغییر فایل / پیام» "
                "به همین دکمه اضافه کنی.\n\n"
                "فایل‌های قبلی هیچ‌وقت جایگزین نمی‌شوند.",
                reply_markup=back_keyboard()
            )

            return True

        # -------------------------------------------------
        # TEXT
        # -------------------------------------------------

        if text == "📝 متن":

            context.user_data["state"] = (
                "add_text"
            )

            await message.reply_text(
                "📝 متن این دکمه را بفرست:",
                reply_markup=back_keyboard()
            )

            return True

        # -------------------------------------------------
        # LINK
        # -------------------------------------------------

        if text == "🔗 لینک":

            context.user_data["state"] = (
                "add_link"
            )

            await message.reply_text(
                "🔗 آدرس لینک را بفرست:\n"
                "مثلاً https://example.com",
                reply_markup=back_keyboard()
            )

            return True

        await message.reply_text(
            "یکی از گزینه‌ها را انتخاب کن."
        )

        return True

    # =====================================================
    # ADD FIRST FILE
    # =====================================================

    if state == "add_file":

        # -----------------------------------------------
        # Create the button ONLY ONCE
        # -----------------------------------------------

        button_id = await create_button(
            title=context.user_data["title"],
            kind="file",
            parent_id=context.user_data.get(
                "parent_id"
            ),
            source_chat_id=message.chat_id,
            source_message_id=message.message_id
        )

        # -----------------------------------------------
        # Save first file in multiple-file table
        # -----------------------------------------------

        await add_file_to_button(
            button_id=button_id,
            source_chat_id=message.chat_id,
            source_message_id=message.message_id
        )

        context.user_data.clear()

        await message.reply_text(
            "✅ دکمه فایل ساخته شد.\n\n"
            "📁 اولین فایل/پیام ثبت شد.\n"
            "➕ هر تعداد فایل دیگری خواستی می‌توانی "
            "بعداً به همین دکمه اضافه کنی.",
            reply_markup=admin_keyboard()
        )

        return True

    # =====================================================
    # CHANGE FILE - SELECT BUTTON
    # =====================================================

    if state == "change_file_choose":

        row = query(
            """
            SELECT id, title, kind
            FROM buttons
            WHERE title = %s
            AND kind = 'file'
            ORDER BY id
            LIMIT 1
            """,
            (text,),
            fetch=True,
            one=True
        )

        if not row:

            await message.reply_text(
                "❌ دکمه فایل پیدا نشد."
            )

            return True

        context.user_data["button_id"] = (
            row["id"]
        )

        context.user_data["state"] = (
            "change_file"
        )

        await message.reply_text(
            "📁 حالا فایل یا پیام جدید را بفرست.\n\n"
            "✅ فایل جدید به فایل‌های قبلی اضافه می‌شود.\n"
            "❌ هیچ فایل قبلی حذف یا جایگزین نمی‌شود.\n\n"
            "هر بار که خواستی می‌توانی دوباره همین گزینه "
            "را انتخاب کنی و فایل دیگری اضافه کنی.",
            reply_markup=back_keyboard()
        )

        return True

    # =====================================================
    # CHANGE FILE - ADD NEW FILE
    # =====================================================

    if state == "change_file":

        button_id = context.user_data.get(
            "button_id"
        )

        if not button_id:

            context.user_data.clear()

            await message.reply_text(
                "❌ خطا در انتخاب دکمه.",
                reply_markup=button_management_keyboard()
            )

            return True

        # -------------------------------------------------
        # IMPORTANT:
        # INSERT instead of UPDATE
        # -------------------------------------------------

        await add_file_to_button(
            button_id=button_id,
            source_chat_id=message.chat_id,
            source_message_id=message.message_id
        )

        context.user_data.clear()

        await message.reply_text(
            "✅ فایل / پیام جدید اضافه شد.\n\n"
            "📁 فایل‌های قبلی همچنان باقی هستند.",
            reply_markup=button_management_keyboard()
        )

        return True

    # =====================================================
    # TEXT
    # =====================================================

    if state == "add_text":

        await create_button(
            title=context.user_data["title"],
            kind="text",
            parent_id=context.user_data.get(
                "parent_id"
            ),
            value=message.text or ""
        )

        context.user_data.clear()

        await message.reply_text(
            "✅ دکمه متنی ساخته شد.",
            reply_markup=admin_keyboard()
        )

        return True

    # =====================================================
    # LINK
    # =====================================================

    if state == "add_link":

        value = (
            message.text or ""
        ).strip()

        if not (
            value.startswith("https://")
            or value.startswith("http://")
        ):

            await message.reply_text(
                "❌ لینک باید با http:// یا https:// شروع شود."
            )

            return True

        await create_button(
            title=context.user_data["title"],
            kind="link",
            parent_id=context.user_data.get(
                "parent_id"
            ),
            value=value
        )

        context.user_data.clear()

        await message.reply_text(
            "✅ دکمه لینک ساخته شد.",
            reply_markup=admin_keyboard()
        )

        return True

    # =====================================================
    # START TEXT
    # =====================================================

    if state == "start_text":

        global START_TEXT_CACHE

        value = (
            message.text or ""
        ).strip()

        if not value:

            await message.reply_text(
                "❌ متن نمی‌تواند خالی باشد."
            )

            return True

        query(
            """
            INSERT INTO settings(key, value)
            VALUES('start_text', %s)
            ON CONFLICT(key)
            DO UPDATE SET value = EXCLUDED.value
            """,
            (value,)
        )

        START_TEXT_CACHE = value

        context.user_data.clear()

        await message.reply_text(
            "✅ متن /start تغییر کرد.",
            reply_markup=admin_keyboard()
        )

        return True

    # =====================================================
    # BROADCAST
    # =====================================================

    if state == "broadcast":

        users = query(
            """
            SELECT user_id
            FROM users
            """,
            fetch=True
        )

        semaphore = asyncio.Semaphore(10)

        async def send_to_user(user):

            async with semaphore:

                try:

                    await context.bot.copy_message(
                        chat_id=user["user_id"],
                        from_chat_id=message.chat_id,
                        message_id=message.message_id
                    )

                    return True

                except Exception as e:

                    logger.warning(
                        "Broadcast failed for %s: %s",
                        user["user_id"],
                        e
                    )

                    return False

        results = await asyncio.gather(
            *[
                send_to_user(user)
                for user in users
            ]
        )

        success = sum(
            1
            for result in results
            if result
        )

        failed = len(results) - success

        context.user_data.clear()

        await message.reply_text(
            "📢 ارسال همگانی تمام شد.\n\n"
            f"✅ موفق: {success}\n"
            f"❌ ناموفق: {failed}",
            reply_markup=admin_keyboard()
        )

        return True

    # =====================================================
    # CHILD MENU
    # =====================================================

    if state == "child_parent":

        row = query(
            """
            SELECT id, title
            FROM buttons
            WHERE title = %s
            AND kind = 'menu'
            ORDER BY id
            LIMIT 1
            """,
            (text,),
            fetch=True,
            one=True
        )

        if not row:

            await message.reply_text(
                "❌ این دکمه منوی فرعی نیست."
            )

            return True

        set_state(
            context,
            "add_name",
            parent_id=row["id"],
            admin_section="button_management"
        )

        await message.reply_text(
            "➕ نام دکمه زیرمجموعه را بفرست:",
            reply_markup=back_keyboard()
        )

        return True

    # =====================================================
    # RENAME CHOOSE
    # =====================================================

    if state == "rename_choose":

        row = query(
            """
            SELECT id, title
            FROM buttons
            WHERE title = %s
            ORDER BY id
            LIMIT 1
            """,
            (text,),
            fetch=True,
            one=True
        )

        if not row:

            await message.reply_text(
                "❌ دکمه پیدا نشد."
            )

            return True

        context.user_data["button_id"] = (
            row["id"]
        )

        context.user_data["state"] = (
            "rename_new"
        )

        await message.reply_text(
            "✏️ نام جدید را بفرست:",
            reply_markup=back_keyboard()
        )

        return True

    # =====================================================
    # RENAME NEW
    # =====================================================

    if state == "rename_new":

        new_title = text.strip()

        if not new_title:

            await message.reply_text(
                "❌ نام نمی‌تواند خالی باشد."
            )

            return True

        query(
            """
            UPDATE buttons
            SET title = %s
            WHERE id = %s
            """,
            (
                new_title,
                context.user_data["button_id"]
            )
        )

        context.user_data.clear()

        await message.reply_text(
            "✅ نام دکمه تغییر کرد.",
            reply_markup=button_management_keyboard()
        )

        return True

    # =====================================================
    # DELETE
    # =====================================================

    if state == "delete_choose":

        row = query(
            """
            SELECT id
            FROM buttons
            WHERE title = %s
            ORDER BY id
            LIMIT 1
            """,
            (text,),
            fetch=True,
            one=True
        )

        if not row:

            await message.reply_text(
                "❌ دکمه پیدا نشد."
            )

            return True

        button_id = row["id"]

        # Delete all files belonging to button first

        query(
            """
            DELETE FROM button_files
            WHERE button_id = %s
            """,
            (button_id,)
        )

        # Delete button

        query(
            """
            DELETE FROM buttons
            WHERE id = %s
            """,
            (button_id,)
        )

        context.user_data.clear()

        await message.reply_text(
            "🗑 دکمه حذف شد.",
            reply_markup=button_management_keyboard()
        )

        return True

    return False


# =========================================================
# ADMIN ROUTER
# =========================================================

async def admin_text_router(update, context):

    if not is_admin(update):
        return False

    if not update.message:
        return False

    text = update.message.text or ""

    state = context.user_data.get(
        "state"
    )

    # =====================================================
    # STATE
    # =====================================================

    if state:

        handled = await handle_admin_state(
            update,
            context
        )

        if handled:
            return True

    # =====================================================
    # BACK WITHOUT STATE
    # =====================================================

    if text == "🔙 لغو / بازگشت":

        section = context.user_data.get(
            "admin_section",
            "main"
        )

        context.user_data.clear()

        # IMPORTANT:
        # Management -> Main Admin Panel

        if section == "button_management":

            await update.message.reply_text(
                "⚙️ پنل مدیریت",
                reply_markup=admin_keyboard()
            )

            return True

        if section == "admin_management":

            await update.message.reply_text(
                "⚙️ پنل مدیریت",
                reply_markup=admin_keyboard()
            )

            return True

        await update.message.reply_text(
            "⚙️ پنل مدیریت",
            reply_markup=admin_keyboard()
        )

        return True

    # =====================================================
    # ADMIN MANAGEMENT
    # =====================================================

    if text == "👥 مدیریت ادمین‌ها":

        await admin_management(
            update,
            context
        )

        return True

    if text == "➕ افزودن ادمین":

        await add_admin_start(
            update,
            context
        )

        return True

    if text == "🗑 حذف ادمین":

        await remove_admin_start(
            update,
            context
        )

        return True

    if text == "👥 لیست ادمین‌ها":

        await admin_list(
            update,
            context
        )

        return True

    # =====================================================
    # BUTTON MANAGEMENT
    # =====================================================

    if text == "➕ افزودن دکمه":

        await add_button_start(
            update,
            context
        )

        return True

    if text == "🛠 مدیریت دکمه‌ها":

        await management_menu(
            update,
            context
        )

        return True

    if text == "✏️ متن /start":

        await edit_start_text(
            update,
            context
        )

        return True

    if text == "📊 آمار کاربران":

        await statistics(
            update,
            context
        )

        return True

    if text == "📢 ارسال همگانی":

        await broadcast_start(
            update,
            context
        )

        return True

    if text == "👤 منوی کاربر":

        await show_root(
            update
        )

        return True

    # =====================================================
    # BUTTON MANAGEMENT ACTIONS
    # =====================================================

    if text == "➕ افزودن دکمه اصلی":

        await add_button_start(
            update,
            context
        )

        return True

    if text == "➕ افزودن زیرمنو":

        await add_child_start(
            update,
            context
        )

        return True

    if text == "✏️ تغییر نام":

        await rename_start(
            update,
            context
        )

        return True

    if text == "📁 تغییر فایل / پیام":

        await change_file_start(
            update,
            context
        )

        return True

    if text == "🗑 حذف دکمه":

        await delete_start(
            update,
            context
        )

        return True

    return False


# =========================================================
# USER ROUTER
# =========================================================

async def user_router(update, context):

    if not update.message:
        return

    if not update.message.text:
        return

    if is_admin(update):

        handled = await admin_text_router(
            update,
            context
        )

        if handled:
            return

    await save_user(update)

    text = update.message.text

    # =====================================================
    # USER BACK
    # =====================================================

    if text == "🔙 بازگشت":

        context.user_data.pop(
            "menu_parent",
            None
        )

        context.user_data.pop(
            "menu_page",
            None
        )

        await show_root(update)

        return

    # =====================================================
    # PAGINATION
    # =====================================================

    if text in (
        "⬅️ صفحه قبل",
        "➡️ صفحه بعد"
    ):

        parent_id = context.user_data.get(
            "menu_parent"
        )

        page = int(
            context.user_data.get(
                "menu_page",
                0
            )
        )

        if text.startswith("⬅️"):

            page = max(
                0,
                page - 1
            )

        else:

            page += 1

        keyboard, _, total = user_keyboard(
            parent_id,
            page
        )

        max_page = max(
            0,
            (total - 1) // 8
        )

        if page > max_page:
            page = max_page

        context.user_data["menu_page"] = page

        keyboard, _, _ = user_keyboard(
            parent_id,
            page
        )

        await update.message.reply_text(
            "📄 صفحه",
            reply_markup=keyboard
        )

        return

    # =====================================================
    # FIND BUTTON
    # =====================================================

    parent_id = context.user_data.get(
        "menu_parent"
    )

    button = query(
        """
        SELECT *
        FROM buttons
        WHERE parent_id IS NOT DISTINCT FROM %s
        AND title = %s
        ORDER BY id
        LIMIT 1
        """,
        (
            parent_id,
            text
        ),
        fetch=True,
        one=True
    )

    if not button:
        return

    # =====================================================
    # SUB MENU
    # =====================================================

    if button["kind"] == "menu":

        context.user_data["menu_parent"] = (
            button["id"]
        )

        context.user_data["menu_page"] = 0

        keyboard, _, _ = user_keyboard(
            button["id"],
            0
        )

        await update.message.reply_text(
            "📂 منوی انتخاب‌شده:",
            reply_markup=keyboard
        )

        return

    # =====================================================
    # MULTIPLE FILES
    # =====================================================

    if button["kind"] == "file":

        files = query(
            """
            SELECT
                source_chat_id,
                source_message_id
            FROM button_files
            WHERE button_id = %s
            ORDER BY id
            """,
            (
                button["id"],
            ),
            fetch=True
        )

        # -------------------------------------------------
        # Fallback for old data
        # -------------------------------------------------

        if not files:

            if (
                button["source_chat_id"] is not None
                and
                button["source_message_id"] is not None
            ):

                files = [
                    {
                        "source_chat_id":
                            button["source_chat_id"],

                        "source_message_id":
                            button["source_message_id"]
                    }
                ]

        if not files:

            await update.message.reply_text(
                "❌ هیچ فایل یا پیامی برای این دکمه ثبت نشده است."
            )

            return

        # -------------------------------------------------
        # Send ALL files
        # -------------------------------------------------

        success = 0
        failed = 0

        for file in files:

            try:

                await context.bot.copy_message(
                    chat_id=update.effective_chat.id,
                    from_chat_id=file[
                        "source_chat_id"
                    ],
                    message_id=file[
                        "source_message_id"
                    ]
                )

                success += 1

            except Exception as e:

                failed += 1

                logger.warning(
                    "File copy failed: %s",
                    e
                )

        if success == 0:

            await update.message.reply_text(
                "❌ فایل‌ها دیگر قابل دریافت نیستند."
            )

        elif failed > 0:

            await update.message.reply_text(
                f"⚠️ {success} فایل ارسال شد و "
                f"{failed} فایل ارسال نشد."
            )

        return

    # =====================================================
    # TEXT
    # =====================================================

    if button["kind"] == "text":

        await update.message.reply_text(
            button["value"] or ""
        )

        return

    # =====================================================
    # LINK
    # =====================================================

    if button["kind"] == "link":

        await update.message.reply_text(
            button["value"] or ""
        )

        return


# =========================================================
# ALL MESSAGES
# =========================================================

async def all_messages(update, context):

    if not update.message:
        return

    if is_admin(update):

        state = context.user_data.get(
            "state"
        )

        # File while creating button
        if state == "add_file":

            handled = await handle_admin_state(
                update,
                context
            )

            if handled:
                return

        # Add another file
        if state == "change_file":

            handled = await handle_admin_state(
                update,
                context
            )

            if handled:
                return

        # Broadcast
        if state == "broadcast":

            handled = await handle_admin_state(
                update,
                context
            )

            if handled:
                return

    await user_router(
        update,
        context
    )


# =========================================================
# ERROR
# =========================================================

async def error_handler(update, context):

    logger.error(
        "Telegram error: %s",
        context.error
    )


# =========================================================
# WEBHOOK
# =========================================================

async def main():

    init_db_pool()

    init_admin_table()

    load_start_text()

    if not PUBLIC_URL:

        raise RuntimeError(
            "WEBHOOK_URL or RENDER_EXTERNAL_URL is required."
        )

    application = (
        Application
        .builder()
        .token(TOKEN)
        .updater(None)
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            all_messages
        )
    )

    application.add_error_handler(
        error_handler
    )

    await application.initialize()

    await application.start()

    webhook_path = (
        "/telegram/"
        + TOKEN
    )

    webhook_url = (
        PUBLIC_URL.rstrip("/")
        + webhook_path
    )

    await application.bot.set_webhook(
        url=webhook_url,
        allowed_updates=Update.ALL_TYPES
    )

    web_app = web.Application()

    async def health(request):

        return web.Response(
            text="OK"
        )

    async def telegram_webhook(request):

        if request.path != webhook_path:

            return web.Response(
                status=404
            )

        data = await request.json()

        update = Update.de_json(
            data=data,
            bot=application.bot
        )

        await application.update_queue.put(
            update
        )

        return web.Response(
            text="OK"
        )

    web_app.router.add_get(
        "/health",
        health
    )

    web_app.router.add_post(
        webhook_path,
        telegram_webhook
    )

    runner = web.AppRunner(
        web_app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    logger.info(
        "FAST BOT STARTED ON PORT %s",
        PORT
    )

    try:

        await asyncio.Event().wait()

    finally:

        await application.bot.delete_webhook()

        await runner.cleanup()

        await application.stop()

        await application.shutdown()

        if db_pool:

            db_pool.close()

            db_pool.wait_closed()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )
