# bot.py
import os
import re
import asyncio
from datetime import datetime
from typing import Optional
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pymongo import MongoClient, ASCENDING
from fuzzywuzzy import process
from flask import Flask
from threading import Thread

# Config from environment
API_ID = int(os.getenv("API_ID", 12345))
API_HASH = os.getenv("API_HASH", "your_api_hash_here")
BOT_TOKEN = os.getenv("BOT_TOKEN", "your_bot_token_here")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", -10012345678))
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "123456789").split(",")))
DATABASE_URL = os.getenv("DATABASE_URL", "mongodb://localhost:27017")
START_PIC = os.getenv("START_PIC", "https://placehold.co/600x400")
RESULTS_COUNT = int(os.getenv("RESULTS_COUNT", 10))

# Pyrogram Client
app = Client("movie_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# MongoDB Connection
mongo = MongoClient(DATABASE_URL)
db = mongo["movie_bot_v2"]
movies_col = db["movies"]
users_col = db["users"]

# Indexing
movies_col.create_index([("title_clean", ASCENDING)], background=True)

# Utils
def clean_text(text: str) -> str:
    return re.sub(r'[^\w]', '', text.lower(), flags=re.UNICODE)

async def delete_message_later(chat_id: int, message_id: int, delay: int = 300):
    await asyncio.sleep(delay)
    try:
        await app.delete_messages(chat_id, message_id)
    except:
        pass

# Language Support
LANGUAGES = {
    "en": {
        "welcome": "🎬 Welcome to Movie Bot!\n\nSend me a movie name to search.",
        "searching": "🔍 Searching...",
        "results": "🎬 Search Results:",
        "no_results": "❌ No results found.",
        "admin_only": "🚫 Admin only feature!"
    },
    "bn": {
        "welcome": "🎬 মুভি বটে স্বাগতম!\n\nমুভির নাম লিখে পাঠান।",
        "searching": "🔍 খোঁজা হচ্ছে...",
        "results": "🎬 সার্চ রেজাল্ট:",
        "no_results": "❌ কিছু খুঁজে পাওয়া যায়নি।",
        "admin_only": "🚫 শুধুমাত্র অ্যাডমিনের জন্য!"
    }
}

# Start command
@app.on_message(filters.command("start"))
async def start_cmd(_, message: Message):
    uid = message.from_user.id
    lang_doc = users_col.find_one({"_id": uid}, {"language": 1}) or {}
    lang = lang_doc.get("language", "bn")

    users_col.update_one(
        {"_id": uid},
        {"$set": {
            "first_name": message.from_user.first_name,
            "username": message.from_user.username,
            "joined": datetime.now(),
            "last_active": datetime.now()
        }},
        upsert=True
    )

    buttons = [
        [InlineKeyboardButton("🔍 সার্চ করুন", switch_inline_query_current_chat="")],
        [InlineKeyboardButton("🌐 ভাষা পরিবর্তন", callback_data="change_language")]
    ]
    if uid in ADMIN_IDS:
        buttons.append([InlineKeyboardButton("🛠️ অ্যাডমিন", callback_data="admin_panel")])

    await message.reply_photo(
        photo=START_PIC,
        caption=LANGUAGES[lang]["welcome"],
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# Text search handler (excluding commands)
@app.on_message(filters.text & ~filters.regex(r"^/"))
async def search_handler(_, message: Message):
    q = message.text.strip()
    uid = message.from_user.id
    if len(q) < 3: return

    lang_doc = users_col.find_one({"_id": uid}, {"language": 1}) or {}
    lang = lang_doc.get("language", "bn")

    status = await message.reply_text(LANGUAGES[lang]["searching"])

    try:
        query_clean = clean_text(q)
        results = list(movies_col.find({
            "$or": [
                {"title_clean": {"$regex": f"^{re.escape(query_clean)}", "$options": "i"}},
                {"title": {"$regex": re.escape(q), "$options": "i"}}
            ]
        }).limit(RESULTS_COUNT))

        if not results:
            titles = [(m["title_clean"], m["message_id"]) for m in movies_col.find({}, {"title_clean": 1, "message_id": 1})]
            matches = process.extract(query_clean, [t[0] for t in titles], limit=5)
            for match in matches:
                if match[1] > 70:
                    msg_id = next(t[1] for t in titles if t[0] == match[0])
                    m = movies_col.find_one({"message_id": msg_id})
                    if m:
                        results.append(m)

        await status.delete()

        if not results:
            return await message.reply_text(LANGUAGES[lang]["no_results"])

        buttons = [[
            InlineKeyboardButton(
                f"{m['title'][:50]} ({m.get('views_count', 0)} 👁️)",
                callback_data=f"send_{m['message_id']}"
            )
        ] for m in results]

        await message.reply_text(LANGUAGES[lang]["results"], reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e:
        await status.edit_text(f"Error: {e}")
        await delete_message_later(message.chat.id, status.id)

# Callback Handler
@app.on_callback_query()
async def cb_handler(_, callback: CallbackQuery):
    data = callback.data
    uid = callback.from_user.id

    if data.startswith("send_"):
        mid = int(data.split("_")[1])
        movie = movies_col.find_one({"message_id": mid})
        if not movie:
            return await callback.answer("❌ মুভিটি পাওয়া যায়নি!", show_alert=True)

        try:
            sent = await app.copy_message(callback.message.chat.id, CHANNEL_ID, mid)
            movies_col.update_one({"message_id": mid}, {"$inc": {"views_count": 1}})

            btns = [[
                InlineKeyboardButton("👍", callback_data=f"like_{mid}"),
                InlineKeyboardButton("👎", callback_data=f"dislike_{mid}")
            ]]
            await callback.message.reply_text("রেটিং দিন:", reply_markup=InlineKeyboardMarkup(btns), reply_to_message_id=sent.id)
        except Exception as e:
            await callback.answer(f"Error: {e}", show_alert=True)

# Flask Health Check
flask_app = Flask(__name__)
@flask_app.route("/")
def home(): return "✅ Movie Bot Running"

def run_flask(): flask_app.run(host="0.0.0.0", port=8080)

# Run bot
if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    print("🚀 Movie Bot Started")
    app.run()
