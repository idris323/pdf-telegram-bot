import os
import asyncio
import logging
import html

from aiohttp import web
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from telegram import (Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)

# =========================================================
# SETTINGS
# =========================================================

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]

# Optional: set TELEGRAM_CHANNEL_ID to a channel chat id such as -1001234567890.
# The bot must be an administrator with permission to post messages.
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")

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

    query("ALTER TABLE buttons ADD COLUMN IF NOT EXISTS hidden BOOLEAN DEFAULT FALSE")
    query("""CREATE TABLE IF NOT EXISTS button_clicks (
        id BIGSERIAL PRIMARY KEY, button_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
        clicked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    query("CREATE INDEX IF NOT EXISTS idx_button_clicks_button ON button_clicks(button_id, clicked_at)")

    # -----------------------------------------------------
    # FILE DATE/PERIOD + USER DOWNLOAD TRACKING
    # -----------------------------------------------------
    query(
        """
        ALTER TABLE button_files
        ADD COLUMN IF NOT EXISTS period VARCHAR(20) DEFAULT 'normal'
        """
    )

    query(
        """
        ALTER TABLE button_files
        ADD COLUMN IF NOT EXISTS added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        """
    )

    query(
        """
        CREATE TABLE IF NOT EXISTS user_file_downloads (
            user_id BIGINT NOT NULL,
            file_id BIGINT NOT NULL,
            downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, file_id)
        )
        """
    )

    query(
        """
        CREATE INDEX IF NOT EXISTS idx_button_files_period
        ON button_files(period, added_at)
        """
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ افزودن دکمه", callback_data="adm:addbtn"), InlineKeyboardButton("🛠 مدیریت دکمه‌ها", callback_data="adm:buttons")],
        [InlineKeyboardButton("📁 مدیریت فایل‌ها", callback_data="adm:files"), InlineKeyboardButton("📊 آمار و گزارش", callback_data="adm:stats")],
        [InlineKeyboardButton("✏️ متن /start", callback_data="adm:starttext"), InlineKeyboardButton("⚙️ تنظیمات", callback_data="adm:settings")],
        [InlineKeyboardButton("📢 ارسال همگانی", callback_data="adm:broadcast"), InlineKeyboardButton("👥 ادمین‌ها", callback_data="adm:admins")],
        [InlineKeyboardButton("👤 پیش‌نمایش کاربر", callback_data="adm:userpreview")]
    ])

def admin_management_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ افزودن ادمین", callback_data="adm:addadmin"), InlineKeyboardButton("🗑 حذف ادمین", callback_data="adm:removeadmin")],
        [InlineKeyboardButton("👥 لیست ادمین‌ها", callback_data="adm:listadmins")],
        [InlineKeyboardButton("🔙 پنل اصلی", callback_data="adm:home")]
    ])

def button_management_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ دکمه اصلی", callback_data="adm:addbtn"), InlineKeyboardButton("➕ زیرمنو", callback_data="adm:addsubmenu")],
        [InlineKeyboardButton("✏️ تغییر نام", callback_data="adm:rename"), InlineKeyboardButton("📁 افزودن فایل/پیام", callback_data="adm:addfile")],
        [InlineKeyboardButton("👁 نمایش/مخفی", callback_data="adm:visibility"), InlineKeyboardButton("↕️ مرتب‌سازی", callback_data="adm:sort")],
        [InlineKeyboardButton("🗑 حذف دکمه", callback_data="adm:deletebtn"), InlineKeyboardButton("📊 آمار دکمه‌ها", callback_data="adm:buttonstats")],
        [InlineKeyboardButton("🔙 پنل اصلی", callback_data="adm:home")]
    ])

def back_keyboard():
    return ReplyKeyboardRemove()


# =========================================================
# USER MENU
# =========================================================

def user_keyboard(parent_id=None, page=0):
    buttons=query("""SELECT id,title,kind FROM buttons
        WHERE parent_id IS NOT DISTINCT FROM %s
          AND COALESCE(hidden,FALSE)=FALSE
        ORDER BY sort_order,id""",(parent_id,),fetch=True)
    per_page=8; start=page*per_page; current=buttons[start:start+per_page]; kb=[]
    if parent_id is None:
        kb += [[InlineKeyboardButton("🆕 تازه‌ترین فایل‌ها",callback_data="uf:latest")],
               [InlineKeyboardButton("📅 امروز",callback_data="uf:today"),InlineKeyboardButton("📆 این هفته",callback_data="uf:week")],
               [InlineKeyboardButton("📥 فایل‌های من",callback_data="uf:mine_menu")],
               [InlineKeyboardButton("🔎 جستجوی فایل",callback_data="uf:search")],
               [InlineKeyboardButton("📥 دانلود همه فایل‌ها",callback_data="uf:all")]]
    for b in current:
        icon="📂" if b["kind"]=="menu" else ("📄" if b["kind"]=="file" else "🔹")
        kb.append([InlineKeyboardButton(f"{icon} {b['title']}",callback_data=f"ub:{b['id']}")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton("⬅️ صفحه قبل",callback_data=f"up:{parent_id or 0}:{page-1}"))
    if start+per_page<len(buttons): nav.append(InlineKeyboardButton("➡️ صفحه بعد",callback_data=f"up:{parent_id or 0}:{page+1}"))
    if nav: kb.append(nav)
    if parent_id is not None: kb.append([InlineKeyboardButton("🔙 بازگشت",callback_data="uback")])
    return InlineKeyboardMarkup(kb),current,len(buttons)


# =========================================================
# FILE FILTER / DOWNLOAD HELPERS
# =========================================================

def mark_file_downloaded(user_id, file_id):
    query(
        """
        INSERT INTO user_file_downloads (user_id, file_id)
        VALUES (%s, %s)
        ON CONFLICT (user_id, file_id) DO NOTHING
        """,
        (user_id, file_id)
    )


def get_filtered_files(mode="latest", user_id=None, limit=None):
    """
    mode:
      latest = newest files
      today = files added today
      week = files added in current week
      mine = files not downloaded by this user
      received = files already sent to this user
      all = all files
    """
    where = ""
    params = []

    if mode == "today":
        where = """
            AND bf.added_at::date = CURRENT_DATE
        """
    elif mode == "week":
        where = """
            AND bf.added_at >= date_trunc('week', CURRENT_TIMESTAMP)
            AND bf.added_at < date_trunc('week', CURRENT_TIMESTAMP) + INTERVAL '7 days'
        """
    elif mode == "mine":
        where = """
            AND %s IS NOT NULL
            AND NOT EXISTS (
                SELECT 1
                FROM user_file_downloads ufd
                WHERE ufd.user_id = %s
                  AND ufd.file_id = bf.id
            )
        """
        params.extend([user_id, user_id])
    elif mode == "received":
        where = """
            AND %s IS NOT NULL
            AND EXISTS (
                SELECT 1
                FROM user_file_downloads ufd
                WHERE ufd.user_id = %s
                  AND ufd.file_id = bf.id
            )
        """
        params.extend([user_id, user_id])

    limit_sql = ""
    if limit:
        limit_sql = " LIMIT %s"
        params.append(limit)

    return query(
        f"""
        SELECT
            bf.id,
            bf.source_chat_id,
            bf.source_message_id,
            bf.added_at,
            bf.period,
            b.title AS button_title
        FROM button_files bf
        JOIN buttons b ON b.id = bf.button_id
        WHERE 1=1
        {where}
        ORDER BY bf.added_at DESC, bf.id DESC
        {limit_sql}
        """,
        tuple(params),
        fetch=True
    )


async def send_file_rows(update, context, rows, heading, mark_download=True):
    if not rows:
        await update.effective_message.reply_text(
            heading + "\n\n❌ فایلی در این بخش وجود ندارد."
        )
        return 0, 0

    success = 0
    failed = 0
    user_id = update.effective_user.id if update.effective_user else None

    await update.effective_message.reply_text(heading)

    for row in rows:
        try:
            await context.bot.copy_message(
                chat_id=update.effective_chat.id,
                from_chat_id=row["source_chat_id"],
                message_id=row["source_message_id"]
            )
            success += 1
            if mark_download and user_id is not None:
                mark_file_downloaded(user_id, row["id"])
            # Keep bulk sending below Telegram's normal burst limit.
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1
            logger.warning(
                "File send failed for file_id=%s: %s",
                row["id"],
                e
            )

    if failed:
        await update.effective_message.reply_text(
            f"✅ ارسال شد: {success}\n"
            f"❌ ارسال نشد: {failed}"
        )

    return success, failed


async def show_file_section(update, context, mode):
    if mode == "latest":
        rows = get_filtered_files("latest", limit=10)
        heading = "🆕 تازه‌ترین فایل‌ها (۱۰ فایل آخر)"
    elif mode == "today":
        rows = get_filtered_files("today")
        heading = "📅 فایل‌های امروز"
    elif mode == "week":
        rows = get_filtered_files("week")
        heading = "📆 فایل‌های این هفته"
    elif mode == "mine":
        rows = get_filtered_files(
            "mine",
            user_id=update.effective_user.id,
            limit=50
        )
        heading = "🆕 فایل‌های دریافت‌نشده شما"
    elif mode == "received":
        rows = get_filtered_files(
            "received",
            user_id=update.effective_user.id,
            limit=50
        )
        heading = "✅ فایل‌های دریافت‌شده شما"
    elif mode == "all":
        rows = get_filtered_files("all")
        heading = "📥 همه فایل‌ها"
    else:
        return

    await send_file_rows(update, context, rows, heading)
    await update.effective_message.reply_text("",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📥 فایل‌های من",callback_data="uf:mine_menu"),InlineKeyboardButton("🏠 خانه",callback_data="uhome")]]))


# =========================================================
# MY FILES MENU
# =========================================================

def my_files_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🆕 فایل‌های جدید",callback_data="uf:mine")],
        [InlineKeyboardButton("📥 دانلود همه فایل‌ها",callback_data="uf:all")],
        [InlineKeyboardButton("✅ فایل‌های دریافت‌شده",callback_data="uf:received")],
        [InlineKeyboardButton("🔙 خانه",callback_data="uhome")]])

async def show_my_files_menu(update,context):
    context.user_data["my_files_menu"]=True
    await update.effective_message.reply_text("📥 فایل‌های من\n\nیکی از گزینه‌ها را انتخاب کن:",reply_markup=my_files_keyboard())


# =========================================================
# ADMIN FILE DELETE
# =========================================================

async def delete_file_start(update, context):
    if not is_admin(update):
        return

    rows = query(
        """
        SELECT
            bf.id,
            bf.added_at,
            b.title AS button_title
        FROM button_files bf
        JOIN buttons b ON b.id = bf.button_id
        ORDER BY bf.added_at DESC, bf.id DESC
        LIMIT 50
        """,
        fetch=True
    )

    if not rows:
        await update.effective_message.reply_text(
            "❌ هیچ فایلی ثبت نشده است.",
            reply_markup=admin_keyboard()
        )
        return

    keyboard = []
    for row in rows:
        title = row["button_title"] or "بدون عنوان"
        keyboard.append([
            KeyboardButton(f"🗑 {row['id']} | {title}")
        ])

    keyboard.append([KeyboardButton("🔙 لغو / بازگشت")])

    set_state(
        context,
        "delete_file_choose",
        admin_section="main"
    )

    await update.effective_message.reply_text(
        "🗑 فایل موردنظر را برای حذف انتخاب کن:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
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

    await update.effective_message.reply_text(
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

    await update.effective_message.reply_text(
        "⚙️ پنل مدیریت ربات",
        reply_markup=admin_keyboard()
    )


# =========================================================
# USER HOME
# =========================================================

async def show_root(update):

    keyboard, _, _ = user_keyboard()

    await update.effective_message.reply_text(
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
# FILE PERIOD KEYBOARD
# =========================================================

def file_period_keyboard():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("📅 امروز"),
                KeyboardButton("📆 این هفته")
            ],
            [
                KeyboardButton("📁 عادی"),
            ],
            [
                KeyboardButton("🔙 لغو / بازگشت")
            ]
        ],
        resize_keyboard=True
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

    await update.effective_message.reply_text(
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

    await update.effective_message.reply_text(
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

        await update.effective_message.reply_text(
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

    await update.effective_message.reply_text(
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

    await update.effective_message.reply_text(
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

        await update.effective_message.reply_text(
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

    await update.effective_message.reply_text(
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

        await update.effective_message.reply_text(
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

    await update.effective_message.reply_text(
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

        await update.effective_message.reply_text(
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

    await update.effective_message.reply_text(
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

    await update.effective_message.reply_text(
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

    await update.effective_message.reply_text(
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

    await update.effective_message.reply_text(
        "📢 حالا هر چیزی که می‌خواهی برای کاربران ارسال شود بفرست.\n\n"
        "متن، عکس، ویدیو، PDF، فایل، صوت و غیره.",
        reply_markup=back_keyboard()
    )


# =========================================================
# ADMIN MANAGEMENT
# =========================================================

async def admin_management(update, context):

    if not is_main_admin(update):

        await update.effective_message.reply_text(
            "❌ فقط ادمین اصلی می‌تواند ادمین‌ها را مدیریت کند."
        )

        return

    context.user_data.clear()

    context.user_data["admin_section"] = (
        "admin_management"
    )

    await update.effective_message.reply_text(
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

    await update.effective_message.reply_text(
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

        await update.effective_message.reply_text(
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

    await update.effective_message.reply_text(
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

        await update.effective_message.reply_text(
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

    await update.effective_message.reply_text(
        text,
        reply_markup=admin_management_keyboard()
    )


# =========================================================
# ADMIN STATE
# =========================================================

async def handle_admin_state(update, context):

    if not is_admin(update):
        return False

    message = update.effective_message

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

        if admin_section == "button_management":

            context.user_data.clear()

            await message.reply_text(
                "⚙️ پنل مدیریت",
                reply_markup=admin_keyboard()
            )

            return True

        if admin_section == "admin_management":

            context.user_data.clear()

            await message.reply_text(
                "⚙️ پنل مدیریت",
                reply_markup=admin_keyboard()
            )

            return True

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

        context.user_data["pending_chat_id"] = message.chat_id
        context.user_data["pending_message_id"] = message.message_id
        context.user_data["state"] = "add_file_period"

        await message.reply_text(
            "📅 این فایل مربوط به کدام بخش است؟\n\n"
            "📅 امروز\n"
            "📆 این هفته\n"
            "📁 عادی",
            reply_markup=file_period_keyboard()
        )

        return True

    # =====================================================
    # ADD FIRST FILE - SELECT PERIOD
    # =====================================================

    if state == "add_file_period":

        period_map = {
            "📅 امروز": "today",
            "📆 این هفته": "week",
            "📁 عادی": "normal"
        }

        period = period_map.get(text)
        if not period:
            await message.reply_text(
                "یکی از گزینه‌های تاریخ را انتخاب کن.",
                reply_markup=file_period_keyboard()
            )
            return True

        button_id = await create_button(
            title=context.user_data["title"],
            kind="file",
            parent_id=context.user_data.get("parent_id"),
            source_chat_id=context.user_data["pending_chat_id"],
            source_message_id=context.user_data["pending_message_id"]
        )

        await add_file_to_button(
            button_id=button_id,
            source_chat_id=context.user_data["pending_chat_id"],
            source_message_id=context.user_data["pending_message_id"]
        )

        query(
            """
            UPDATE button_files
            SET period = %s
            WHERE button_id = %s
              AND source_chat_id = %s
              AND source_message_id = %s
            """,
            (
                period,
                button_id,
                context.user_data["pending_chat_id"],
                context.user_data["pending_message_id"]
            )
        )

        context.user_data.clear()

        await message.reply_text(
            "✅ دکمه فایل ساخته شد و فایل ثبت شد.",
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

        context.user_data["state"] = "change_file_period"

        await message.reply_text(
            "📅 نوع انتشار فایل‌های جدید را انتخاب کن.\n\n"
            "بعد از انتخاب، هر تعداد فایل که بفرستی به همین دکمه اضافه می‌شود.",
            reply_markup=file_period_keyboard()
        )

        return True

    # =====================================================
    # CHANGE FILE - SELECT PERIOD
    # =====================================================

    if state == "change_file_period":

        period_map = {
            "📅 امروز": "today",
            "📆 این هفته": "week",
            "📁 عادی": "normal"
        }

        period = period_map.get(text)
        if not period:
            await message.reply_text(
                "یکی از گزینه‌های تاریخ را انتخاب کن.",
                reply_markup=file_period_keyboard()
            )
            return True

        context.user_data["file_period"] = period
        context.user_data["state"] = "change_file"

        await message.reply_text(
            "📁 حالا فایل‌ها را بفرست.\n\n"
            "✅ فایل‌های قبلی حذف نمی‌شوند.\n"
            "➕ هر فایل جدید به همین دکمه اضافه می‌شود.\n\n"
            "🔙 وقتی تمام شد «لغو / بازگشت» را بزن.",
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
        # ADD EVERY NEW MESSAGE AS A NEW FILE
        # -------------------------------------------------

        await add_file_to_button(
            button_id=button_id,
            source_chat_id=message.chat_id,
            source_message_id=message.message_id
        )

        query(
            """
            UPDATE button_files
            SET period = %s
            WHERE button_id = %s
              AND source_chat_id = %s
              AND source_message_id = %s
            """,
            (
                context.user_data.get("file_period", "normal"),
                button_id,
                message.chat_id,
                message.message_id
            )
        )

        # -------------------------------------------------
        # IMPORTANT:
        # DO NOT CLEAR context.user_data
        #
        # The state remains "change_file",
        # so the next selected file is also saved.
        # -------------------------------------------------

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

        channel_result = ""
        if CHANNEL_ID:
            try:
                await context.bot.copy_message(
                    chat_id=int(CHANNEL_ID),
                    from_chat_id=message.chat_id,
                    message_id=message.message_id
                )
                channel_result = "\n📣 کانال: ✅ ارسال شد"
            except Exception as e:
                logger.warning("Channel broadcast failed: %s", e)
                channel_result = "\n📣 کانال: ❌ ارسال نشد"

        context.user_data.clear()

        await message.reply_text(
            "📢 ارسال همگانی تمام شد.\n\n"
            f"✅ موفق: {success}\n"
            f"❌ ناموفق: {failed}"
            f"{channel_result}",
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
    # DELETE FILE
    # =====================================================

    if state == "delete_file_choose":

        if not text.startswith("🗑 "):
            await message.reply_text(
                "❌ فایل معتبر را انتخاب کن."
            )
            return True

        try:
            file_id = int(text.split("|", 1)[0].replace("🗑", "").strip())
        except Exception:
            await message.reply_text("❌ شناسه فایل نامعتبر است.")
            return True

        row = query(
            """
            SELECT id
            FROM button_files
            WHERE id = %s
            """,
            (file_id,),
            fetch=True,
            one=True
        )

        if not row:
            await message.reply_text("❌ فایل پیدا نشد.")
            return True

        query(
            """
            DELETE FROM user_file_downloads
            WHERE file_id = %s
            """,
            (file_id,)
        )

        query(
            """
            DELETE FROM button_files
            WHERE id = %s
            """,
            (file_id,)
        )

        context.user_data.clear()

        await message.reply_text(
            "✅ فایل با موفقیت حذف شد.",
            reply_markup=admin_keyboard()
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

        query(
            """
            DELETE FROM button_files
            WHERE button_id = %s
            """,
            (button_id,)
        )

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

    if not update.effective_message:
        return False

    text = update.effective_message.text or ""

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

        if section == "button_management":

            await update.effective_message.reply_text(
                "⚙️ پنل مدیریت",
                reply_markup=admin_keyboard()
            )

            return True

        if section == "admin_management":

            await update.effective_message.reply_text(
                "⚙️ پنل مدیریت",
                reply_markup=admin_keyboard()
            )

            return True

        await update.effective_message.reply_text(
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

    if text == "🗑 حذف فایل":

        await delete_file_start(
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


    if context.user_data.get("state") == "sort_button":
        try: order=int(text.strip())
        except ValueError: await message.reply_text("❌ فقط عدد وارد کن."); return True
        query("UPDATE buttons SET sort_order=%s WHERE id=%s",(order,context.user_data.get("sort_button_id")))
        context.user_data.clear(); context.user_data["admin_section"]="button_management"; await message.reply_text("✅ ترتیب ذخیره شد.",reply_markup=button_management_keyboard()); return True

# =========================================================
# USER ROUTER
# =========================================================

async def user_router(update, context):

    if not update.effective_message:
        return

    if not update.effective_message.text:
        return

    if is_admin(update):

        handled = await admin_text_router(
            update,
            context
        )

        if handled:
            return

    await save_user(update)

    text = update.effective_message.text

    if context.user_data.get("state") == "search_files":
        term=(text or "").strip()
        if not term: await update.effective_message.reply_text("❌ عبارت جستجو خالی است."); return
        rows=query("SELECT bf.id,bf.source_chat_id,bf.source_message_id,b.title AS button_title FROM button_files bf JOIN buttons b ON b.id=bf.button_id WHERE b.title ILIKE %s OR COALESCE(b.value,'') ILIKE %s ORDER BY bf.added_at DESC LIMIT 30",(f"%{term}%",f"%{term}%"),fetch=True)
        context.user_data.clear(); await send_file_rows(update,context,rows,f"🔎 نتایج جستجو: {term}"); await update.effective_message.reply_text("",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه",callback_data="uhome")]])); return

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

        await update.effective_message.reply_text(
            "📄 صفحه",
            reply_markup=keyboard
        )

        return

    # =====================================================
    # FIXED FILE SHORTCUTS
    # =====================================================

    if text == "🆕 تازه‌ترین فایل‌ها":
        await show_file_section(update, context, "latest")
        return

    if text == "📅 فایل‌های امروز":
        await show_file_section(update, context, "today")
        return

    if text == "📆 فایل‌های این هفته":
        await show_file_section(update, context, "week")
        return

    if text == "📥 فایل‌های من":
        await show_my_files_menu(update, context)
        return

    if text == "🆕 فایل‌های جدید":
        await show_file_section(update, context, "mine")
        return

    if text == "📥 دانلود همه فایل‌ها":
        await show_file_section(update, context, "all")
        return

    if text == "✅ فایل‌های دریافت‌شده":
        await show_file_section(update, context, "received")
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

        await update.effective_message.reply_text(
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

        if not files:

            await update.effective_message.reply_text(
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
                if update.effective_user:
                    # Find the database file row and remember this user received it.
                    db_file = query(
                        """
                        SELECT id
                        FROM button_files
                        WHERE button_id = %s
                          AND source_chat_id = %s
                          AND source_message_id = %s
                        LIMIT 1
                        """,
                        (
                            button["id"],
                            file["source_chat_id"],
                            file["source_message_id"]
                        ),
                        fetch=True,
                        one=True
                    )
                    if db_file:
                        mark_file_downloaded(
                            update.effective_user.id,
                            db_file["id"]
                        )

            except Exception as e:

                failed += 1

                logger.warning(
                    "File copy failed: %s",
                    e
                )

        if success == 0:

            await update.effective_message.reply_text(
                "❌ فایل‌ها دیگر قابل دریافت نیستند."
            )

        elif failed > 0:

            await update.effective_message.reply_text(
                f"⚠️ {success} فایل ارسال شد و "
                f"{failed} فایل ارسال نشد."
            )

        return

    # =====================================================
    # TEXT
    # =====================================================

    if button["kind"] == "text":

        await update.effective_message.reply_text(
            button["value"] or ""
        )

        return

    # =====================================================
    # LINK
    # =====================================================

    if button["kind"] == "link":

        await update.effective_message.reply_text(
            button["value"] or ""
        )

        return


# =========================================================
# INLINE CALLBACKS
# =========================================================
async def callback_router(update, context):
    q=update.callback_query
    if not q: return
    data=q.data or ""
    if data=="uhome":
        await q.answer(); context.user_data.clear(); kb,_,_=user_keyboard(); await q.message.reply_text(get_start_text(),reply_markup=kb); return
    if data=="uf:mine_menu":
        await q.answer(); await show_my_files_menu(update,context); return
    if data.startswith("uf:"):
        mode=data.split(":",1)[1]; await q.answer()
        if mode=="search":
            context.user_data.clear(); context.user_data["state"]="search_files"; await q.message.reply_text("🔎 نام مضمون، عنوان یا کلمه موردنظر را بفرست:",reply_markup=ReplyKeyboardRemove()); return
        if mode in ("latest","today","week","mine","received","all"):
            await show_file_section(update,context,mode); return
    if data=="uback":
        await q.answer(); context.user_data["menu_parent"]=None; context.user_data["menu_page"]=0; kb,_,_=user_keyboard(); await q.message.reply_text(get_start_text(),reply_markup=kb); return
    if data.startswith("up:"):
        await q.answer(); _,pid,page=data.split(":",2); pid=None if pid=="0" else int(pid); kb,_,_=user_keyboard(pid,int(page)); await q.message.reply_text("📄 صفحه:",reply_markup=kb); return
    if data.startswith("ub:"):
        await q.answer(); bid=int(data.split(":",1)[1]); row=query("SELECT * FROM buttons WHERE id=%s AND COALESCE(hidden,FALSE)=FALSE",(bid,),fetch=True,one=True)
        if not row: await q.message.reply_text("❌ این دکمه دیگر موجود نیست."); return
        if update.effective_user: query("INSERT INTO button_clicks(button_id,user_id) VALUES(%s,%s)",(bid,update.effective_user.id))
        if row["kind"]=="menu":
            context.user_data["menu_parent"]=bid; context.user_data["menu_page"]=0; kb,_,_=user_keyboard(bid,0); await q.message.reply_text("📂 "+(row["title"] or "منو"),reply_markup=kb); return
        if row["kind"] in ("text","link"):
            await q.message.reply_text(row["value"] or ""); return
        if row["kind"]=="file":
            files=query("SELECT id,source_chat_id,source_message_id FROM button_files WHERE button_id=%s ORDER BY id",(bid,),fetch=True); ok=0
            for f in files:
                try:
                    await context.bot.copy_message(q.message.chat_id,f["source_chat_id"],f["source_message_id"]); ok+=1
                    if update.effective_user: mark_file_downloaded(update.effective_user.id,f["id"])
                    await asyncio.sleep(.05)
                except Exception as e: logger.warning("Inline file copy failed: %s",e)
            await q.message.reply_text(f"✅ {ok} فایل ارسال شد."); return
    if not is_admin(update): await q.answer("دسترسی ندارید.",show_alert=True); return
    await q.answer()
    if data=="adm:home": context.user_data.clear(); await q.message.reply_text("⚙️ پنل پیشرفته مدیریت",reply_markup=admin_keyboard()); return
    if data=="adm:buttons": context.user_data.clear(); context.user_data["admin_section"]="button_management"; await q.message.reply_text("🛠 مدیریت پیشرفته دکمه‌ها",reply_markup=button_management_keyboard()); return
    if data=="adm:files":
        r=query("SELECT COUNT(*) AS total,COUNT(*) FILTER(WHERE added_at::date=CURRENT_DATE) AS today,COUNT(*) FILTER(WHERE added_at>=date_trunc('week',CURRENT_TIMESTAMP)) AS week FROM button_files",fetch=True,one=True)
        await q.message.reply_text(f"📁 مدیریت فایل‌ها\\n\\n📦 کل: {r['total']}\\n📅 امروز: {r['today']}\\n📆 این هفته: {r['week']}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ افزودن فایل",callback_data="adm:addfile")],[InlineKeyboardButton("🗑 حذف فایل",callback_data="adm:deletefile")],[InlineKeyboardButton("🔙 پنل اصلی",callback_data="adm:home")]])); return
    if data=="adm:stats":
        r=query("SELECT COUNT(*) n FROM users",fetch=True,one=True); b=query("SELECT COUNT(*) n FROM buttons",fetch=True,one=True); f=query("SELECT COUNT(*) n FROM button_files",fetch=True,one=True); c=query("SELECT COUNT(*) n FROM button_clicks",fetch=True,one=True)
        await q.message.reply_text(f"📊 گزارش کلی\\n\\n👥 کاربران: {r['n']}\\n🧩 دکمه‌ها: {b['n']}\\n📄 فایل‌ها: {f['n']}\\n🖱 کلیک‌ها: {c['n']}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 پنل اصلی",callback_data="adm:home")]])); return
    if data=="adm:settings":
        await q.message.reply_text("⚙️ تنظیمات پیشرفته",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👁 نمایش/مخفی دکمه",callback_data="adm:visibility")],[InlineKeyboardButton("↕️ مرتب‌سازی دکمه‌ها",callback_data="adm:sort")],[InlineKeyboardButton("📊 آمار دکمه‌ها",callback_data="adm:buttonstats")],[InlineKeyboardButton("🔙 پنل اصلی",callback_data="adm:home")]])); return
    if data=="adm:userpreview": kb,_,_=user_keyboard(); await q.message.reply_text("👤 پیش‌نمایش منوی کاربر",reply_markup=kb); return
    if data=="adm:starttext": set_state(context,"start_text",admin_section="main"); await q.message.reply_text("✏️ متن جدید /start را بفرست:",reply_markup=ReplyKeyboardRemove()); return
    if data=="adm:broadcast": set_state(context,"broadcast",admin_section="main"); await q.message.reply_text("📢 پیام همگانی را بفرست:",reply_markup=ReplyKeyboardRemove()); return
    if data=="adm:addbtn": set_state(context,"add_name",parent_id=None,admin_section="button_management"); await q.message.reply_text("➕ نام دکمه را بفرست:",reply_markup=ReplyKeyboardRemove()); return
    if data=="adm:addsubmenu": await add_child_start(update,context); return
    if data=="adm:rename": await rename_start(update,context); return
    if data=="adm:addfile": await change_file_start(update,context); return
    if data=="adm:deletebtn": await delete_start(update,context); return
    if data=="adm:deletefile": await delete_file_start(update,context); return
    if data=="adm:addadmin": await add_admin_start(update,context); return
    if data=="adm:removeadmin": await remove_admin_start(update,context); return
    if data=="adm:listadmins": await admin_list(update,context); return
    if data=="adm:admins": context.user_data.clear(); context.user_data["admin_section"]="admin_management"; await q.message.reply_text("👥 مدیریت ادمین‌ها",reply_markup=admin_management_keyboard()); return
    if data=="adm:buttonstats":
        rows=query("SELECT b.title,COUNT(c.id) clicks FROM buttons b LEFT JOIN button_clicks c ON c.button_id=b.id GROUP BY b.id ORDER BY clicks DESC,b.id DESC LIMIT 30",fetch=True); text="📊 آمار دکمه‌ها\\n\\n"+"\\n".join(f"{i}. {r['title']} — {r['clicks']} کلیک" for i,r in enumerate(rows,1)) if rows else "❌ دکمه‌ای وجود ندارد."; await q.message.reply_text(text,reply_markup=button_management_keyboard()); return
    if data=="adm:visibility":
        rows=query("SELECT id,title,COALESCE(hidden,FALSE) hidden FROM buttons ORDER BY parent_id NULLS FIRST,sort_order,id",fetch=True); kb=[[InlineKeyboardButton(("🚫 " if r['hidden'] else "✅ ")+str(r['title']),callback_data=f"btv:{r['id']}")] for r in rows[:50]]+[ [InlineKeyboardButton("🔙 برگشت",callback_data="adm:buttons")] ]; await q.message.reply_text("👁 وضعیت نمایش را انتخاب کن:",reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("btv:"):
        bid=int(data.split(":",1)[1]); query("UPDATE buttons SET hidden=NOT COALESCE(hidden,FALSE) WHERE id=%s",(bid,)); await q.message.reply_text("✅ وضعیت نمایش تغییر کرد.",reply_markup=button_management_keyboard()); return
    if data=="adm:sort":
        rows=query("SELECT id,title,sort_order FROM buttons ORDER BY parent_id NULLS FIRST,sort_order,id",fetch=True); kb=[[InlineKeyboardButton(f"↕️ {r['title']} ({r['sort_order']})",callback_data=f"bts:{r['id']}")] for r in rows[:50]]+[[InlineKeyboardButton("🔙 برگشت",callback_data="adm:buttons")]]; await q.message.reply_text("دکمه را انتخاب کن:",reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("bts:"):
        bid=int(data.split(":",1)[1]); context.user_data.clear(); context.user_data.update({"state":"sort_button","sort_button_id":bid,"admin_section":"button_management"}); await q.message.reply_text("↕️ شماره ترتیب جدید را بفرست:",reply_markup=ReplyKeyboardRemove()); return

# =========================================================
# ALL MESSAGES
# =========================================================

async def all_messages(update, context):

    if not update.effective_message:
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

    application.add_handler(CallbackQueryHandler(callback_router))

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
