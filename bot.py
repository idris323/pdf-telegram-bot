import os
import sqlite3
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB = "bot.db"


# ================= DATABASE =================

def db():
    return sqlite3.connect(DB)


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            file_id TEXT NOT NULL
        )
    """)

    con.commit()
    con.close()


def add_user(user_id):
    con = db()
    con.execute(
        "INSERT OR IGNORE INTO users(user_id) VALUES(?)",
        (user_id,)
    )
    con.commit()
    con.close()


def get_files():
    con = db()
    rows = con.execute(
        "SELECT id, name FROM files ORDER BY id DESC"
    ).fetchall()
    con.close()
    return rows


def get_file(file_id):
    con = db()
    row = con.execute(
        "SELECT name, file_id FROM files WHERE id=?",
        (file_id,)
    ).fetchone()
    con.close()
    return row


def save_file(name, file_id):
    con = db()
    con.execute(
        "INSERT INTO files(name, file_id) VALUES(?, ?)",
        (name, file_id)
    )
    con.commit()
    con.close()


def delete_file(file_id):
    con = db()
    con.execute(
        "DELETE FROM files WHERE id=?",
        (file_id,)
    )
    con.commit()
    con.close()


def get_users():
    con = db()
    rows = con.execute("SELECT user_id FROM users").fetchall()
    con.close()
    return [r[0] for r in rows]


# ================= USER MENU =================

def user_menu():
    keyboard = [
        [InlineKeyboardButton("📚 فایل‌های PDF", callback_data="files")],
        [
            InlineKeyboardButton("ℹ️ درباره ربات", callback_data="about"),
            InlineKeyboardButton("📞 پشتیبانی", callback_data="support"),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    add_user(user.id)

    await update.message.reply_text(
        "👋 سلام!\n\n"
        "به کتابخانه PDF خوش آمدید.\n"
        "از منوی زیر فایل مورد نظر خود را انتخاب کنید:",
        reply_markup=user_menu()
    )


# ================= FILE LIST =================

async def show_files(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    files = get_files()

    if not files:
        await query.edit_message_text(
            "📂 هنوز هیچ فایلی اضافه نشده است.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 برگشت", callback_data="home")]
            ])
        )
        return

    keyboard = []

    for file_id, name in files:
        keyboard.append([
            InlineKeyboardButton(
                "📄 " + name,
                callback_data=f"send_{file_id}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton("🔙 برگشت", callback_data="home")
    ])

    await query.edit_message_text(
        "📚 فایل‌های موجود:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ================= SEND PDF =================

async def send_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    file_id = int(query.data.split("_")[1])

    data = get_file(file_id)

    if not data:
        await query.message.reply_text("❌ فایل پیدا نشد.")
        return

    name, telegram_file_id = data

    await query.message.reply_document(
        document=telegram_file_id,
        caption=f"📄 {name}"
    )


# ================= ADMIN PANEL =================

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != ADMIN_ID:
        return

    keyboard = [
        [InlineKeyboardButton("➕ افزودن PDF", callback_data="add")],
        [InlineKeyboardButton("📂 مدیریت فایل‌ها", callback_data="manage")],
        [InlineKeyboardButton("👥 آمار کاربران", callback_data="stats")],
        [InlineKeyboardButton("📢 ارسال همگانی", callback_data="broadcast")],
    ]

    await update.message.reply_text(
        "🔐 پنل مدیریت\n\n"
        "یکی از گزینه‌ها را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ================= ADD PDF =================

async def add_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    context.user_data["adding_pdf"] = True

    await query.message.reply_text(
        "📄 PDF را همینجا برای من ارسال کن.\n\n"
        "بعد از ارسال، نام فایل را از تو می‌پرسم."
    )


async def receive_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != ADMIN_ID:
        return

    if not context.user_data.get("adding_pdf"):
        return

    if not update.message.document:
        return

    document = update.message.document

    if document.mime_type != "application/pdf":
        await update.message.reply_text(
            "❌ لطفاً فقط فایل PDF ارسال کن."
        )
        return

    context.user_data["pdf_file_id"] = document.file_id
    context.user_data["adding_pdf"] = False
    context.user_data["waiting_name"] = True

    await update.message.reply_text(
        "✅ PDF دریافت شد.\n\n"
        "حالا نامی که می‌خواهی روی دکمه نمایش داده شود را بفرست."
    )


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != ADMIN_ID:
        return

    if not context.user_data.get("waiting_name"):
        return

    name = update.message.text
    file_id = context.user_data.get("pdf_file_id")

    if not file_id:
        return

    save_file(name, file_id)

    context.user_data["waiting_name"] = False
    context.user_data["pdf_file_id"] = None

    await update.message.reply_text(
        f"✅ فایل با موفقیت اضافه شد.\n\n"
        f"📄 نام: {name}"
    )


# ================= MANAGE FILES =================

async def manage(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    files = get_files()

    if not files:
        await query.message.reply_text("📂 فایلی وجود ندارد.")
        return

    keyboard = []

    for file_id, name in files:
        keyboard.append([
            InlineKeyboardButton(
                "🗑 " + name,
                callback_data=f"delete_{file_id}"
            )
        ])

    await query.message.reply_text(
        "📂 برای حذف فایل روی آن بزن:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def delete_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    file_id = int(query.data.split("_")[1])

    delete_file(file_id)

    await query.message.reply_text(
        "✅ فایل حذف شد."
    )


# ================= STATISTICS =================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    users = len(get_users())
    files = len(get_files())

    await query.message.reply_text(
        "📊 آمار ربات\n\n"
        f"👥 کاربران: {users}\n"
        f"📚 فایل‌ها: {files}"
    )


# ================= BROADCAST =================

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    context.user_data["broadcast"] = True

    await query.message.reply_text(
        "📢 پیام همگانی\n\n"
        "پیامی که می‌خواهی برای کاربران ارسال شود را بفرست."
    )


async def receive_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != ADMIN_ID:
        return

    if not context.user_data.get("broadcast"):
        return

    text = update.message.text

    context.user_data["broadcast"] = False

    users = get_users()

    success = 0

    for user_id in users:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=text
            )
            success += 1
            await asyncio.sleep(0.05)

        except Exception:
            pass

    await update.message.reply_text(
        f"✅ پیام ارسال شد.\n\n"
        f"👥 تعداد ارسال موفق: {success}"
    )


# ================= BUTTONS =================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if query.data == "files":
        await show_files(update, context)

    elif query.data.startswith("send_"):
        await send_pdf(update, context)

    elif query.data == "home":

        await query.edit_message_text(
            "🏠 منوی اصلی:",
            reply_markup=user_menu()
        )

    elif query.data == "about":

        await query.edit_message_text(
            "ℹ️ درباره ربات\n\n"
            "این ربات برای دریافت فایل‌های PDF ساخته شده است.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 برگشت", callback_data="home")]
            ])
        )

    elif query.data == "support":

        await query.edit_message_text(
            "📞 پشتیبانی\n\n"
            "برای پشتیبانی با مدیر ربات تماس بگیرید.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 برگشت", callback_data="home")]
            ])
        )

    elif query.data == "add":
        await add_pdf(update, context)

    elif query.data == "manage":
        await manage(update, context)

    elif query.data.startswith("delete_"):
        await delete_pdf(update, context)

    elif query.data == "stats":
        await stats(update, context)

    elif query.data == "broadcast":
        await broadcast(update, context)


# ================= MAIN =================

def main():

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))

    app.add_handler(CallbackQueryHandler(buttons))

    app.add_handler(
        MessageHandler(
            filters.Document.PDF,
            receive_pdf
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_name
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_broadcast
        )
    )

    print("Bot started...")

    app.run_polling()


if __name__ == "__main__":
    main()
