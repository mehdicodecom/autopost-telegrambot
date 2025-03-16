import time
import logging
import telegram
from googletrans import Translator
from telegram import Bot
from telegram.ext import Updater, CallbackContext
import requests

# Configuration
BOT_TOKEN = "8066804842:AAElJ6wdRUBuVc5PK-gH1x09RMDhOego9kg"
CHANNELS_TO_MONITOR = ["@iMTProto"]  # List of source channels
TARGET_CHANNEL = "@tsetrmandf"  # Your channel
CHECK_INTERVAL = 600  # Check every 10 minutes (600 seconds)

bot = Bot(token=BOT_TOKEN)
translator = Translator()
latest_messages = {}

logging.basicConfig(level=logging.INFO)

def get_channel_updates(channel):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        response = requests.get(url).json()
        messages = []

        if "result" in response:
            for update in response["result"]:
                if "channel_post" in update:
                    post = update["channel_post"]
                    if post.get("chat", {}).get("username") == channel.replace("@", ""):
                        messages.append(post["text"])
        return messages
    except Exception as e:
        logging.error(f"Error fetching updates: {e}")
        return []

def translate_text(text, dest_lang='fa'):
    try:
        translated = translator.translate(text, dest=dest_lang)
        return translated.text
    except Exception as e:
        logging.error(f"Translation failed: {e}")
        return text

def check_and_forward_messages():
    global latest_messages
    for channel in CHANNELS_TO_MONITOR:
        messages = get_channel_updates(channel)

        for message in messages:
            if message not in latest_messages.get(channel, []):
                translated_message = translate_text(message)
                bot.send_message(chat_id=TARGET_CHANNEL, text=translated_message)
                latest_messages.setdefault(channel, []).append(message)

    time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    while True:
        check_and_forward_messages()