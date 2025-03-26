import asyncio
import logging
import os
import uuid
from telethon import TelegramClient, events
from telegram.ext import Application
from telegram.request import HTTPXRequest
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument
import telegram.error
import mimetypes
import re
from asyncio import Lock
from telethon.tl.types import MessageMediaWebPage, DocumentAttributeAudio
from telegram.helpers import escape_markdown
from dotenv import load_dotenv
load_dotenv()

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Credentials
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
PHONE = os.getenv('PHONE')
BOT_TOKEN = os.getenv('BOT_TOKEN')
FORBIDDEN_WORDS = ['تبلیغات', 'advertisement', 'buy now', 'تبلیغاتی']

# Target configurations
TARGET_CONFIGS = {
    -1002547551677: {
        "monitored_channels": [-1001365323499, -1001565746460, -1001366910165, -1001704831156, -1001958818083, -1002611129623],
        "my_channel": "EasylionerNews"
    },
#     -1002326590160: {
#         "monitored_channels": [-1002611129623, -1001003587533],
#         "my_channel": "another_channel"
#     }
}

# Initialize Telethon client
client = TelegramClient('session_name', API_ID, API_HASH)

# Initialize bot with custom timeouts
request = HTTPXRequest(connect_timeout=30, read_timeout=30, connection_pool_size=50)
application = Application.builder().token(BOT_TOKEN).request(request).build()

# Track last processed message IDs and processed media groups per target and monitored channel
last_processed_ids = {
    target_channel: {monitored_channel: 0 for monitored_channel in config["monitored_channels"]}
    for target_channel, config in TARGET_CONFIGS.items()
}
processed_media_groups = {
    target_channel: set() for target_channel in TARGET_CONFIGS.keys()
}

# Mapping from monitored channels to target channels
MONITORED_TO_TARGET = {
    monitored_channel: target_channel
    for target_channel, config in TARGET_CONFIGS.items()
    for monitored_channel in config["monitored_channels"]
}

# Locks for each monitored channel to prevent race conditions
locks = {monitored_channel: Lock() for monitored_channel in MONITORED_TO_TARGET.keys()}

# Helper function to get file extension
def get_file_extension(mime_type):
    if mime_type:
        ext = mimetypes.guess_extension(mime_type)
        if ext:
            return ext.lstrip('.')
    return 'bin'

# Escape special characters for MarkdownV2
def escape_markdown_v2(text):
    return escape_markdown(text, version=2)

# Process text with MarkdownV2 support
def process_text(text, my_channel):
    if not text:
        return None
    if any(word.lower() in text.lower() for word in FORBIDDEN_WORDS):
        return None
    text = re.sub(r'@\w+', f'@{my_channel}', text)
    parts = re.split(r'(\[.*?\]\(.*?\))', text)
    processed_parts = []
    for part in parts:
        if match := re.match(r'\[(.*?)\]\((.*?)\)', part):
            link_text = escape_markdown_v2(match.group(1))
            link_url = match.group(2)
            processed_parts.append(f'[{link_text}]({link_url})')
        else:
            processed_parts.append(escape_markdown_v2(part))
    return ''.join(processed_parts)

# Retry function for API calls
async def retry_api_call(api_func, *args, **kwargs):
    attempt = 0
    max_attempts = 10
    while attempt < max_attempts:
        attempt += 1
        try:
            return await api_func(*args, **kwargs)
        except telegram.error.TimedOut as e:
            logger.error(f"Timeout on attempt {attempt}: {e}")
            if attempt == max_attempts:
                raise
            await asyncio.sleep(min(2 ** attempt, 30))
        except telegram.error.BadRequest as e:
            logger.error(f"Bad request on attempt {attempt}: {e}")
            if "File must be non-empty" in str(e):
                raise
            if attempt == max_attempts:
                raise
            await asyncio.sleep(min(2 ** attempt, 30))
        except Exception as e:
            logger.error(f"Error on attempt {attempt}: {e}")
            if attempt == max_attempts:
                raise
            await asyncio.sleep(min(2 ** attempt, 30))

# Retry function for downloading media
async def download_media_with_retry(message, file_path):
    attempt = 0
    max_attempts = 5
    while attempt < max_attempts:
        attempt += 1
        try:
            await client.download_media(message, file=file_path)
            await asyncio.sleep(3)
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                logger.info(f"Media downloaded to {file_path} after {attempt} attempts")
                return True
            else:
                logger.error(f"Download attempt {attempt} failed: File empty or missing")
                if attempt == max_attempts:
                    return False
                await asyncio.sleep(min(2 ** attempt, 30))
        except Exception as e:
            logger.error(f"Download attempt {attempt} failed: {e}")
            if attempt == max_attempts:
                return False
            await asyncio.sleep(min(2 ** attempt, 30))
    return False

# Process a message (handles both single messages and media groups)
async def process_message(message, target_channel):
    monitored_channel = message.chat_id
    lock = locks[monitored_channel]

    async with lock:
        if message.id <= last_processed_ids[target_channel][monitored_channel]:
            return

        config = TARGET_CONFIGS[target_channel]
        my_channel = config["my_channel"]
        processed_text = process_text(message.text, my_channel) if message.text else None

        if processed_text is None and message.text:
            logger.info("Message contains forbidden words, skipping")
            return

        if message.grouped_id:
            await process_media_group(message, target_channel, processed_text)
            processed_media_groups[target_channel].add(message.grouped_id)
            last_processed_ids[target_channel][monitored_channel] = message.id  # Update last processed ID
        else:
            await process_single_message(message, target_channel, processed_text)
            last_processed_ids[target_channel][monitored_channel] = message.id

# Process media groups
async def process_media_group(message, target_channel, processed_text):
    media_group_id = message.grouped_id
    monitored_channel = message.chat_id
    messages = await client.get_messages(monitored_channel, limit=10, min_id=message.id - 10, max_id=message.id + 10)
    media_group_messages = [msg for msg in messages if msg.grouped_id == media_group_id]

    media_inputs = []
    temp_files = []  # Track temporary file paths
    for idx, msg in enumerate(media_group_messages):
        caption = processed_text if idx == 0 else None
        if msg.photo:
            file_path = f'photo_{uuid.uuid4()}.jpg'
            if await download_media_with_retry(msg, file_path):
                temp_files.append(file_path)
                media_inputs.append(InputMediaPhoto(media=open(file_path, 'rb'), caption=caption, parse_mode='MarkdownV2'))
        elif msg.video:
            file_path = f'video_{uuid.uuid4()}.mp4'
            if await download_media_with_retry(msg, file_path):
                temp_files.append(file_path)
                media_inputs.append(InputMediaVideo(media=open(file_path, 'rb'), caption=caption, parse_mode='MarkdownV2'))
        elif msg.document:
            mime_type = msg.document.mime_type
            ext = get_file_extension(mime_type)
            file_path = f'doc_{uuid.uuid4()}.{ext}'
            if await download_media_with_retry(msg, file_path):
                temp_files.append(file_path)
                if mime_type and mime_type.startswith('image/'):
                    media_inputs.append(InputMediaPhoto(media=open(file_path, 'rb'), caption=caption, parse_mode='MarkdownV2'))
                elif mime_type and mime_type.startswith('video/'):
                    media_inputs.append(InputMediaVideo(media=open(file_path, 'rb'), caption=caption, parse_mode='MarkdownV2'))
                else:
                    media_inputs.append(InputMediaDocument(media=open(file_path, 'rb'), caption=caption, parse_mode='MarkdownV2'))

    if media_inputs:
        try:
            await retry_api_call(
                application.bot.send_media_group,
                chat_id=str(target_channel),
                media=media_inputs
            )
            logger.info(f"Media group {media_group_id} sent with {len(media_inputs)} items")
        except Exception as e:
            logger.error(f"Failed to send media group {media_group_id}: {e}")
        finally:
            for file_path in temp_files:
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Deleted temp file: {file_path}")
                except Exception as e:
                    logger.error(f"Failed to delete temp file {file_path}: {e}")

# Process single messages
async def process_single_message(message, target_channel, processed_text):
    file_path = None
    try:
        if message.media:
            if isinstance(message.media, MessageMediaWebPage):
                if message.media.webpage:
                    url = message.media.webpage.url
                    processed_text = processed_text + f"\n\n[Web Page Preview]({url})" if processed_text else f"[Web Page Preview]({url})"
            elif message.photo:
                file_path = f'photo_{uuid.uuid4()}.jpg'
                if await download_media_with_retry(message, file_path):
                    with open(file_path, 'rb') as f:
                        await retry_api_call(
                            application.bot.send_photo,
                            chat_id=str(target_channel),
                            photo=f,
                            caption=processed_text,
                            parse_mode='MarkdownV2'
                        )
                    logger.info("Photo sent")
            elif message.video:
                file_path = f'video_{uuid.uuid4()}.mp4'
                if await download_media_with_retry(message, file_path):
                    with open(file_path, 'rb') as f:
                        await retry_api_call(
                            application.bot.send_video,
                            chat_id=str(target_channel),
                            video=f,
                            caption=processed_text,
                            parse_mode='MarkdownV2'
                        )
                    logger.info("Video sent")
            elif message.voice:
                file_path = f'voice_{uuid.uuid4()}.ogg'
                if await download_media_with_retry(message, file_path):
                    with open(file_path, 'rb') as f:
                        await retry_api_call(
                            application.bot.send_voice,
                            chat_id=str(target_channel),
                            voice=f,
                            caption=processed_text,
                            parse_mode='MarkdownV2'
                        )
                    logger.info("Voice message sent")
            elif message.document:
                mime_type = message.document.mime_type
                ext = get_file_extension(mime_type)
                file_path = f'doc_{uuid.uuid4()}.{ext}'
                if await download_media_with_retry(message, file_path):
                    with open(file_path, 'rb') as f:
                        if mime_type and mime_type.startswith('audio/'):
                            title = None
                            for attr in message.document.attributes:
                                if isinstance(attr, DocumentAttributeAudio):
                                    title = attr.title
                                    break
                            await retry_api_call(
                                application.bot.send_audio,
                                chat_id=str(target_channel),
                                audio=f,
                                caption=processed_text,
                                parse_mode='MarkdownV2',
                                title=title
                            )
                            logger.info("Audio sent with original title")
                        elif any(attr.sticker for attr in message.document.attributes):
                            await retry_api_call(
                                application.bot.send_sticker,
                                chat_id=str(target_channel),
                                sticker=f
                            )
                            logger.info("Sticker sent")
                        elif mime_type == 'image/gif':
                            await retry_api_call(
                                application.bot.send_animation,
                                chat_id=str(target_channel),
                                animation=f,
                                caption=processed_text,
                                parse_mode='MarkdownV2'
                            )
                            logger.info("GIF sent")
                        elif mime_type and mime_type.startswith('image/'):
                            await retry_api_call(
                                application.bot.send_photo,
                                chat_id=str(target_channel),
                                photo=f,
                                caption=processed_text,
                                parse_mode='MarkdownV2'
                            )
                            logger.info("Image document sent")
                        else:
                            await retry_api_call(
                                application.bot.send_document,
                                chat_id=str(target_channel),
                                document=f,
                                caption=processed_text,
                                parse_mode='MarkdownV2'
                            )
                            logger.info("Document sent")
        elif processed_text:
            await retry_api_call(
                application.bot.send_message,
                chat_id=str(target_channel),
                text=processed_text,
                parse_mode='MarkdownV2'
            )
            logger.info("Text message sent")
    except Exception as e:
        logger.error(f"Failed to process message {message.id}: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Deleted temp file: {file_path}")
            except Exception as e:
                logger.error(f"Failed to delete temp file {file_path}: {e}")

# Initialize last processed IDs
async def initialize_last_processed_ids():
    for target_channel, config in TARGET_CONFIGS.items():
        for monitored_channel in config["monitored_channels"]:
            try:
                messages = await client.get_messages(monitored_channel, limit=1)
                if messages:
                    last_processed_ids[target_channel][monitored_channel] = messages[0].id
                    logger.info(f"Initialized last processed ID for {monitored_channel}: {messages[0].id}")
                else:
                    logger.warning(f"No messages in {monitored_channel}, starting from 0")
            except Exception as e:
                logger.error(f"Failed to initialize {monitored_channel}: {e}")

# Event handler for new messages
@client.on(events.NewMessage(chats=list(MONITORED_TO_TARGET.keys())))
async def handler(event):
    monitored_channel = event.message.chat_id
    target_channel = MONITORED_TO_TARGET[monitored_channel]
    logger.info(f"New message in {monitored_channel}: ID {event.message.id}")
    await process_message(event.message, target_channel)

# Periodic task to check missed messages
async def check_missed_messages():
    while True:
        try:
            for target_channel, config in TARGET_CONFIGS.items():
                for monitored_channel in config["monitored_channels"]:
                    last_id = last_processed_ids[target_channel][monitored_channel]
                    messages = await client.get_messages(monitored_channel, min_id=last_id, limit=100)
                    for message in reversed(messages):
                        await process_message(message, target_channel)
        except Exception as e:
            logger.error(f"Error checking missed messages: {e}")
        await asyncio.sleep(300)  # Check every 5 minutes

# Send startup message
async def send_startup_message():
    for target_channel in TARGET_CONFIGS.keys():
        try:
            await retry_api_call(
                application.bot.send_message,
                chat_id=str(target_channel),
                text="Bot started\\. Monitoring target channels\\.",
                parse_mode='MarkdownV2'
            )
            logger.info(f"Startup message sent to {target_channel}")
        except Exception as e:
            logger.error(f"Failed to send startup message to {target_channel}: {e}")

# Main function
async def main():
    await client.start(PHONE)
    logger.info(f"Logged in as user ID: {(await client.get_me()).id}")
    await initialize_last_processed_ids()
    await send_startup_message()
    asyncio.create_task(check_missed_messages())
    await client.run_until_disconnected()
    logger.warning("Client disconnected")

if __name__ == '__main__':
    asyncio.run(main())