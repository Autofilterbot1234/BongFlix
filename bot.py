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

# Configuration
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))
VIP_IDS = list(map(int, os.getenv("VIP_IDS", "").split(",")))
DATABASE_URL = os.getenv("DATABASE_URL")
UPDATE_CHANNEL = os.getenv("UPDATE_CHANNEL", "https://t.me/CTGMovieOfficial")
START_PIC = os.getenv("START_PIC", "https://i.ibb.co/prnGXMr3/photo-2025-05-16-05-15-45-7504908428624527364.jpg")
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
settings_col = db["settings"]

# Indexes
movies_col.create_index([("title_clean", ASCENDING)], background=True)
movies_col.create_index([("language", ASCENDING)], background=True)
movies_col.create_index([("views_count", ASCENDING)], background=True)
movies_col.create_index([("avg_rating", ASCENDING)], background=True)

# Flask for health check
flask_app = Flask(__name__)
@flask_app.route('/')
def home():
    return "Bot is running!"
Thread(target=lambda: flask_app.run(host='0.0.0.0', port=8080)).start()

# Helper Functions
def clean_text(text: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]', '', text.lower())

def extract_language(text: str) -> Optional[str]:
    langs = ["Bengali", "Hindi", "English", "Tamil", "Telugu"]
    return next((lang for lang in langs if lang.lower() in text.lower()), None)

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
        "no_results": "❌ কিছু খুঁজে পাওয়া যায়নি।",
        "admin_only": "🚫 শুধুমাত্র অ্যাডমিনের জন্য!"
    }
}

# Start Command
@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    user_id = message.from_user.id
    lang = users_col.find_one({"_id": user_id}, {"language": 1}).get("language", "bn")
    
    # Update user data
    users_col.update_one(
        {"_id": user_id},
        {"$set": {"joined": datetime.now(), "last_active": datetime.now()},
         "$setOnInsert": {"favorite_movies": [], "language": "bn"}},
        upsert=True
    )

    # Handle deep links (e.g., /start watch_12345)
    if len(message.command) > 1:
        if message.command[1].startswith("watch_"):
            return await handle_watch_deeplink(message)
    
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

async def handle_watch_deeplink(message: Message):
    movie_id = int(message.command[1].split("_")[1])
    movie = movies_col.find_one({"message_id": movie_id})
    
    if not movie:
        return await message.reply("❌ মুভিটি পাওয়া যায়নি!")
    
    # Send movie
    sent_msg = await app.copy_message(
        chat_id=message.chat.id,
        from_chat_id=CHANNEL_ID,
        message_id=movie_id,
        protect_content=True
    )
    
    # Update view count
    movies_col.update_one(
        {"message_id": movie_id},
        {"$inc": {"views_count": 1}}
    )
    
    # Prepare rating buttons
    buttons = [
        [
            InlineKeyboardButton(f"👍 {movie.get('likes', 0)}", callback_data=f"like_{movie_id}"),
            InlineKeyboardButton(f"👎 {movie.get('dislikes', 0)}", callback_data=f"dislike_{movie_id}")
        ],
        [InlineKeyboardButton("⭐ ফেভারিটে যোগ করুন", callback_data=f"fav_{movie_id}")]
    ]
    
    await message.reply_text(
        "এই মুভিটি কেমন লাগলো? রেটিং দিন:",
        reply_markup=InlineKeyboardMarkup(buttons),
        reply_to_message_id=sent_msg.id
    )

# Search Functionality
@app.on_message(filters.text & ~filters.command)
async def handle_search(client: Client, message: Message):
    query = message.text.strip()
    if len(query) < 3:
        return
    
    user_id = message.from_user.id
    lang = users_col.find_one({"_id": user_id}, {"language": 1}).get("language", "bn")
    
    # Show searching message
    search_msg = await message.reply_text(LANGUAGES[lang]["searching"])
    
    # Search in database
    query_clean = clean_text(query)
    results = list(movies_col.find(
        {"$or": [
            {"title_clean": {"$regex": f"^{re.escape(query_clean)}", "$options": "i"}},
            {"title": {"$regex": re.escape(query), "$options": "i"}}
        ]}
    ).limit(RESULTS_COUNT))
    
    if not results:
        # Fuzzy search if no direct matches
        all_titles = [(m["title_clean"], m["message_id"]) for m in movies_col.find({}, {"title_clean": 1, "message_id": 1})]
        matches = process.extract(query_clean, [t[0] for t in all_titles], limit=5)
        
        results = []
        for match in matches:
            if match[1] > 70:  # 70% similarity threshold
                msg_id = next(t[1] for t in all_titles if t[0] == match[0])
                movie = movies_col.find_one({"message_id": msg_id})
                if movie:
                    results.append(movie)
    
    # Delete searching message
    await search_msg.delete()
    
    if not results:
        return await message.reply_text(LANGUAGES[lang]["no_results"])
    
    # Prepare results
    buttons = []
    for movie in results[:10]:  # Max 10 results
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

# Callback Handlers
@app.on_callback_query()
async def handle_callbacks(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    user_id = callback_query.from_user.id
    
    if data.startswith("send_"):
        movie_id = int(data.split("_")[1])
        await send_movie(user_id, movie_id, callback_query.message.id)
    
    elif data.startswith("like_") or data.startswith("dislike_"):
        movie_id = int(data.split("_")[1])
        await handle_rating(user_id, movie_id, data.startswith("like_"), callback_query)
    
    elif data.startswith("fav_"):
        movie_id = int(data.split("_")[1])
        await toggle_favorite(user_id, movie_id, callback_query)
    
    elif data == "favorites":
        await show_favorites(user_id, callback_query)
    
    elif data == "admin_panel":
        if user_id in ADMIN_IDS:
            await show_admin_panel(callback_query)
        else:
            await callback_query.answer("🚫 Access denied!", show_alert=True)
    
    elif data.startswith("lang_"):
        lang = data.split("_")[1]
        users_col.update_one({"_id": user_id}, {"$set": {"language": lang}})
        await callback_query.answer(f"Language set to {lang}!")
        await callback_query.message.delete()

async def send_movie(user_id: int, movie_id: int, reply_to: int):
    movie = movies_col.find_one({"message_id": movie_id})
    if not movie:
        return
    
    # Send movie
    await app.copy_message(
        chat_id=user_id,
        from_chat_id=CHANNEL_ID,
        message_id=movie_id,
        reply_to_message_id=reply_to
    )
    
    # Update view count
    movies_col.update_one(
        {"message_id": movie_id},
        {"$inc": {"views_count": 1}}
    )

async def handle_rating(user_id: int, movie_id: int, is_like: bool, callback: CallbackQuery):
    # Check if already rated
    if movies_col.find_one({"message_id": movie_id, "rated_by": user_id}):
        return await callback.answer("আপনি ইতিমধ্যেই রেটিং দিয়েছেন!", show_alert=True)
    
    # Update rating
    field = "likes" if is_like else "dislikes"
    movies_col.update_one(
        {"message_id": movie_id},
        {"$inc": {field: 1}, "$push": {"rated_by": user_id}}
    )
    
    # Update message buttons
    movie = movies_col.find_one({"message_id": movie_id})
    buttons = [
        [
            InlineKeyboardButton(f"👍 {movie.get('likes', 0)}", callback_data=f"like_{movie_id}"),
            InlineKeyboardButton(f"👎 {movie.get('dislikes', 0)}", callback_data=f"dislike_{movie_id}")
        ],
        [InlineKeyboardButton("⭐ ফেভারিটে যোগ করুন", callback_data=f"fav_{movie_id}")]
    ]
    
    await callback.message.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    await callback.answer("ধন্যবাদ আপনার রেটিং এর জন্য!")

async def toggle_favorite(user_id: int, movie_id: int, callback: CallbackQuery):
    user = users_col.find_one({"_id": user_id})
    favorites = user.get("favorite_movies", [])
    
    if movie_id in favorites:
        users_col.update_one(
            {"_id": user_id},
            {"$pull": {"favorite_movies": movie_id}}
        )
        await callback.answer("ফেভারিট থেকে সরানো হয়েছে ❌", show_alert=True)
    else:
        users_col.update_one(
            {"_id": user_id},
            {"$addToSet": {"favorite_movies": movie_id}}
        )
        await callback.answer("ফেভারিটে যোগ করা হয়েছে ⭐", show_alert=True)

async def show_favorites(user_id: int, callback: CallbackQuery):
    user = users_col.find_one({"_id": user_id})
    if not user or not user.get("favorite_movies"):
        return await callback.answer("আপনার ফেভারিট তালিকায় কিছু নেই!", show_alert=True)
    
    favorites = movies_col.find({"message_id": {"$in": user["favorite_movies"]}})
    
    buttons = []
    for movie in favorites:
        buttons.append([
            InlineKeyboardButton(
                movie["title"][:50],
                callback_data=f"send_{movie['message_id']}"
            )
        ])
        buttons.append([
            InlineKeyboardButton(
                f"❌ {movie['title'][:20]}... সরান",
                callback_data=f"fav_{movie['message_id']}"
            )
        ])
    
    await callback.message.edit_text(
        "❤️ আপনার ফেভারিট মুভিগুলো:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def show_admin_panel(callback: CallbackQuery):
    buttons = [
        [InlineKeyboardButton("📊 স্ট্যাটিস্টিক্স", callback_data="admin_stats")],
        [InlineKeyboardButton("📢 ব্রডকাস্ট", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🗑️ মুভি ডিলিট", callback_data="admin_delete")],
        [InlineKeyboardButton("🔙 ব্যাক", callback_data="back_to_main")]
    ]
    
    await callback.message.edit_text(
        "🛠️ অ্যাডমিন কন্ট্রোল প্যানেল",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# Admin Commands
@app.on_message(filters.command("broadcast") & filters.user(ADMIN_IDS))
async def broadcast_message(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply("ব্যবহার: /broadcast <message>")
    
    text = message.text.split(maxsplit=1)[1]
    users = users_col.find({})
    count = 0
    
    for user in users:
        try:
            await app.send_message(user["_id"], text)
            count += 1
            await asyncio.sleep(0.1)  # Prevent flooding
        except Exception:
            continue
    
    await message.reply(f"✅ {count} জন ব্যবহারকারীর কাছে মেসেজ পাঠানো হয়েছে!")

@app.on_message(filters.command("stats") & filters.user(ADMIN_IDS))
async def show_stats(client: Client, message: Message):
    stats = {
        "users": users_col.count_documents({}),
        "movies": movies_col.count_documents({}),
        "requests": requests_col.count_documents({}),
        "active": users_col.count_documents({"last_active": {"$gt": datetime.now() - timedelta(days=7)}})
    }
    
    await message.reply(
        "📊 বট স্ট্যাটিস্টিক্স:\n\n"
        f"• মোট ইউজার: {stats['users']}\n"
        f"• মোট মুভি: {stats['movies']}\n"
        f"• সক্রিয় ইউজার (৭ দিন): {stats['active']}\n"
        f"• পেন্ডিং রিকোয়েস্ট: {stats['requests']}"
    )

# Run the bot
if __name__ == "__main__":
    print("🚀 Starting Movie Bot...")
    app.run()
