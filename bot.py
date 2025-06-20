import os
import re
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from bson.objectid import ObjectId
from fuzzywuzzy import process
import urllib.parse
from threading import Thread
from flask import Flask
import requests

# Configuration (এনভায়রনমেন্ট ভেরিয়েবল দিয়ে প্রতিস্থাপন করুন)
API_ID = int(os.getenv("API_ID", 12345))
API_HASH = os.getenv("API_HASH", "your_api_hash_here")
BOT_TOKEN = os.getenv("BOT_TOKEN", "your_bot_token_here")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", -10012345678))
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "123456789").split(",")))
VIP_IDS = list(map(int, os.getenv("VIP_IDS", "987654321").split(",")))
DATABASE_URL = os.getenv("DATABASE_URL", "mongodb://localhost:27017")
UPDATE_CHANNEL = os.getenv("UPDATE_CHANNEL", "https://t.me/yourchannel")
START_PIC = os.getenv("START_PIC", "https://example.com/start.jpg")
RESULTS_COUNT = int(os.getenv("RESULTS_COUNT", 10))

# Initialize
app = Client("movie_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
mongo = MongoClient(DATABASE_URL)
db = mongo["movie_bot_v2"]

# Collections
movies_col = db["movies"]
users_col = db["users"]
requests_col = db["requests"]
feedback_col = db["feedback"]
stats_col = db["stats"]

# Indexes
movies_col.create_index([("title_clean", ASCENDING)], background=True)
movies_col.create_index([("language", ASCENDING)], background=True)

# Helper Functions
def clean_text(text: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]', '', text.lower())

def extract_language(text: str) -> Optional[str]:
    langs = ["Bengali", "Hindi", "English", "Tamil", "Telugu"]
    text_lower = text.lower()
    return next((lang for lang in langs if lang.lower() in text_lower), None)

def extract_year(text: str) -> Optional[int]:
    match = re.search(r'\b(19|20)\d{2}\b', text)
    return int(match.group(0)) if match else None

async def delete_message_later(chat_id: int, message_id: int, delay: int = 300):
    await asyncio.sleep(delay)
    try:
        await app.delete_messages(chat_id, message_id)
    except Exception:
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

# Start Command
@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    user_id = message.from_user.id
    lang = users_col.find_one({"_id": user_id}, {"language": 1}) or {}
    lang = lang.get("language", "bn")
    
    # Update user data
    users_col.update_one(
        {"_id": user_id},
        {"$set": {
            "first_name": message.from_user.first_name,
            "last_name": message.from_user.last_name,
            "username": message.from_user.username,
            "joined": datetime.now(),
            "last_active": datetime.now()
        }},
        upsert=True
    )

    # Prepare buttons
    buttons = [
        [InlineKeyboardButton("🔍 সার্চ করুন", switch_inline_query_current_chat="")],
        [InlineKeyboardButton("⭐ ফেভারিট", callback_data="favorites"),
         InlineKeyboardButton("📊 স্ট্যাটাস", callback_data="stats")],
        [InlineKeyboardButton("🌐 ভাষা পরিবর্তন", callback_data="change_language")]
    ]
    
    if user_id in ADMIN_IDS:
        buttons.append([InlineKeyboardButton("🛠️ অ্যাডমিন প্যানেল", callback_data="admin_panel")])
    
    await message.reply_photo(
        photo=START_PIC,
        caption=LANGUAGES[lang]["welcome"],
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# Search Handler (ফিক্সড ফিল্টার সিনট্যাক্স)
@app.on_message(filters.text & ~filters.command())
async def handle_search(client: Client, message: Message):
    query = message.text.strip()
    if len(query) < 3:
        return
    
    user_id = message.from_user.id
    lang = users_col.find_one({"_id": user_id}, {"language": 1}) or {}
    lang = lang.get("language", "bn")
    
    search_msg = await message.reply_text(LANGUAGES[lang]["searching"])
    
    try:
        # Search in database
        query_clean = clean_text(query)
        results = list(movies_col.find(
            {"$or": [
                {"title_clean": {"$regex": f"^{re.escape(query_clean)}", "$options": "i"}},
                {"title": {"$regex": re.escape(query), "$options": "i"}}
            ]}
        ).limit(RESULTS_COUNT))
        
        if not results:
            # Fuzzy search fallback
            all_titles = [(m["title_clean"], m["message_id"]) for m in movies_col.find({}, {"title_clean": 1, "message_id": 1})]
            matches = process.extract(query_clean, [t[0] for t in all_titles], limit=5)
            
            results = []
            for match in matches:
                if match[1] > 70:
                    msg_id = next(t[1] for t in all_titles if t[0] == match[0])
                    movie = movies_col.find_one({"message_id": msg_id})
                    if movie:
                        results.append(movie)
        
        await search_msg.delete()
        
        if not results:
            return await message.reply_text(LANGUAGES[lang]["no_results"])
        
        # Prepare results
        buttons = []
        for movie in results[:10]:
            buttons.append([
                InlineKeyboardButton(
                    f"{movie['title'][:50]} ({movie.get('views_count', 0)} 👁️)",
                    callback_data=f"send_{movie['message_id']}"
                )
            ])
        
        await message.reply_text(
            LANGUAGES[lang]["results"],
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        await search_msg.edit_text(f"Error: {str(e)}")
        await delete_message_later(message.chat.id, search_msg.id)

# Callback Handlers
@app.on_callback_query()
async def handle_callbacks(client: Client, callback: CallbackQuery):
    data = callback.data
    user_id = callback.from_user.id
    
    if data.startswith("send_"):
        movie_id = int(data.split("_")[1])
        movie = movies_col.find_one({"message_id": movie_id})
        
        if not movie:
            return await callback.answer("❌ মুভিটি পাওয়া যায়নি!", show_alert=True)
        
        try:
            sent_msg = await app.copy_message(
                chat_id=callback.message.chat.id,
                from_chat_id=CHANNEL_ID,
                message_id=movie_id,
                reply_to_message_id=callback.message.id
            )
            
            movies_col.update_one(
                {"message_id": movie_id},
                {"$inc": {"views_count": 1}}
            )
            
            buttons = [
                [
                    InlineKeyboardButton(f"👍 {movie.get('likes', 0)}", callback_data=f"like_{movie_id}"),
                    InlineKeyboardButton(f"👎 {movie.get('dislikes', 0)}", callback_data=f"dislike_{movie_id}")
                ],
                [InlineKeyboardButton("⭐ ফেভারিটে যোগ করুন", callback_data=f"fav_{movie_id}")]
            ]
            
            await callback.message.reply_text(
                "এই মুভিটি কেমন লাগলো? রেটিং দিন:",
                reply_markup=InlineKeyboardMarkup(buttons),
                reply_to_message_id=sent_msg.id
            )
        except Exception as e:
            await callback.answer(f"Error: {str(e)}", show_alert=True)

# Flask Health Check
flask_app = Flask(__name__)
@flask_app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    flask_app.run(host='0.0.0.0', port=8080)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    print("🚀 Starting Movie Bot...")
    app.run()
