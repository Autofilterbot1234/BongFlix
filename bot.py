from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pymongo import MongoClient, ASCENDING
from pymongo.errors import OperationFailure, CollectionInvalid, DuplicateKeyError
from flask import Flask
from threading import Thread
import os
import re
from datetime import datetime, UTC, timedelta 
import asyncio
import urllib.parse
from fuzzywuzzy import process
from concurrent.futures import ThreadPoolExecutor

# Configs
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
RESULTS_COUNT = int(os.getenv("RESULTS_COUNT", 10))
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))
DATABASE_URL = os.getenv("DATABASE_URL")
UPDATE_CHANNEL = os.getenv("UPDATE_CHANNEL", "https://t.me/CTGMovieOfficial")
START_PIC = os.getenv("START_PIC", "https://i.ibb.co/prnGXMr3/photo-2025-05-16-05-15-45-7504908428624527364.jpg")

app = Client("movie_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# MongoDB setup
mongo = MongoClient(DATABASE_URL)
db = mongo["movie_bot"]
movies_col = db["movies"]
feedback_col = db["feedback"]
stats_col = db["stats"]
users_col = db["users"]
settings_col = db["settings"]
requests_col = db["requests"]

# Indexing
try:
    movies_col.create_index("message_id", unique=True, background=True)
    movies_col.create_index("language", background=True)
    movies_col.create_index([("title_clean", ASCENDING)], background=True)
    movies_col.create_index([("language", ASCENDING), ("title_clean", ASCENDING)], background=True)
    movies_col.create_index([("views_count", ASCENDING)], background=True)
except Exception as e:
    print(f"Error creating indexes: {e}")

# Flask App for health check
flask_app = Flask(__name__)
@flask_app.route("/")
def home():
    return "Bot is running!"
Thread(target=lambda: flask_app.run(host="0.0.0.0", port=8080)).start()

# Helpers
def clean_text(text):
    return re.sub(r'[^a-zA-Z0-9]', '', text.lower())

def extract_language(text):
    langs = ["Bengali", "Hindi", "English"]
    return next((lang for lang in langs if lang.lower() in text.lower()), None)

def extract_year(text):
    match = re.search(r'\b(19|20)\d{2}\b', text)
    return int(match.group(0)) if match else None

async def delete_message_later(chat_id, message_id, delay=300):
    await asyncio.sleep(delay)
    try:
        await app.delete_messages(chat_id, message_id)
    except Exception as e:
        print(f"Error deleting message: {e}")

# New function to send notifications with posters
async def send_notification_to_users(original_msg, movie_text, poster_id):
    setting = settings_col.find_one({"key": "global_notify"})
    if not setting or not setting.get("value"):
        return

    short_text = movie_text.split('\n')[0][:100]
    notification_text = f"🎬 নতুন মুভি আপলোড হয়েছে!\n\n{short_text}"

    for user in users_col.find({"notify": True}):
        try:
            if poster_id:
                await app.send_photo(
                    chat_id=user["_id"],
                    photo=poster_id,
                    caption=notification_text,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "ডাউনলোড করুন",
                            url=f"https://t.me/{app.me.username}?start=watch_{original_msg.id}"
                        )
                    ]])
                )
            else:
                await app.send_message(
                    chat_id=user["_id"],
                    text=notification_text,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "ডাউনলোড করুন",
                            url=f"https://t.me/{app.me.username}?start=watch_{original_msg.id}"
                        )
                    ]])
                )
            await asyncio.sleep(0.1)
        except Exception as e:
            if "USER_IS_BLOCKED" in str(e):
                users_col.update_one({"_id": user["_id"]}, {"$set": {"notify": False}})
            print(f"Failed to notify user {user['_id']}: {e}")

# Updated save_post handler with poster support
@app.on_message(filters.chat(CHANNEL_ID))
async def save_post(_, msg: Message):
    text = msg.text or msg.caption
    if not text:
        return

    # Extract poster if available
    poster_id = None
    if msg.photo:
        poster_id = msg.photo.file_id
    elif msg.document and msg.document.mime_type.startswith('image/'):
        poster_id = msg.document.file_id

    movie_data = {
        "message_id": msg.id,
        "title": text.split('\n')[0],
        "full_text": text,
        "date": msg.date,
        "year": extract_year(text),
        "language": extract_language(text),
        "title_clean": clean_text(text),
        "poster_id": poster_id,
        "views_count": 0,
        "likes": 0,
        "dislikes": 0,
        "rated_by": []
    }

    movies_col.update_one({"message_id": msg.id}, {"$set": movie_data}, upsert=True)
    await send_notification_to_users(msg, text, poster_id)

# Updated start command with poster support
@app.on_message(filters.command("start"))
async def start(_, msg: Message):
    user_id = msg.from_user.id
    users_col.update_one(
        {"_id": user_id},
        {"$set": {"joined": datetime.now(UTC), "notify": True}},
        upsert=True
    )

    if len(msg.command) > 1 and msg.command[1].startswith("watch_"):
        message_id = int(msg.command[1].split("_")[1])
        movie = movies_col.find_one({"message_id": message_id})
        
        if not movie:
            return await msg.reply("⚠️ মুভিটি খুঁজে পাওয়া যায়নি")

        try:
            if movie.get("poster_id"):
                sent_msg = await app.send_photo(
                    chat_id=msg.chat.id,
                    photo=movie["poster_id"],
                    caption=movie["full_text"]
                )
            else:
                sent_msg = await app.copy_message(
                    chat_id=msg.chat.id,
                    from_chat_id=CHANNEL_ID,
                    message_id=message_id
                )

            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"👍 লাইক ({movie.get('likes', 0)})",
                    callback_data=f"like_{message_id}_{user_id}"
                ),
                InlineKeyboardButton(
                    f"👎 ডিসলাইক ({movie.get('dislikes', 0)})",
                    callback_data=f"dislike_{message_id}_{user_id}"
                )
            ]])
            
            await app.send_message(
                chat_id=msg.chat.id,
                text="রেটিং দিন ⭐",
                reply_to_message_id=sent_msg.id,
                reply_markup=markup
            )

            movies_col.update_one(
                {"message_id": message_id},
                {"$inc": {"views_count": 1}}
            )

        except Exception as e:
            print(f"Error showing movie: {e}")
            await msg.reply("⚠️ মুভিটি লোড করতে সমস্যা হয়েছে")

    else:
        await msg.reply_photo(
            photo=START_PIC,
            caption="মুভির নাম লিখে সার্চ করুন 🎬",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("আপডেট চ্যানেল", url="https://t.me/CTGMovieSupport")],
                [InlineKeyboardButton("সাপোর্ট গ্রুপ", url="https://t.me/CTGMovieSupport")]
            ])
        )

# [Rest of your existing handlers...]
# Keep all your other handlers (search, feedback, stats, etc.) unchanged 
# from your original code as they were working fine

if __name__ == "__main__":
    print("বট শুরু হচ্ছে...")
    app.run()
