import asyncio
import logging
import os
import tempfile
import re
import shutil
from urllib.parse import urlparse
import socket
import ipaddress

import yt_dlp
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import TelegramError

# ========================= CONFIG =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", 8443))

MAX_FILE_SIZE = 400_000_000
SAFE_DOWNLOAD_LIMIT = 380_000_000

MAX_CONCURRENT = 3
AUTO_DELETE_SECONDS = 37 * 60

FORMAT_SELECTOR = (
    "best[height<=720][filesize<=198000000]/"
    "best[height<=480][filesize<=400000000]/"
    "best[height<=720]/best[height<=480]/best"
)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

YDL_OPTIONS = {
    "format": FORMAT_SELECTOR,
    "outtmpl": "%(title).80s-%(id)s.%(ext)s",
    "noplaylist": True,
    "max_filesize": SAFE_DOWNLOAD_LIMIT,
    "restrictfilenames": True,
    "quiet": False,
    "socket_timeout": 30,
    "retries": 8,
    "fragment_retries": 8,
    "user_agent": USER_AGENT,
    "http_headers": HTTP_HEADERS,
    "skip_unavailable_fragments": True,
    "extractor_args": {"generic": {"skip_unavailable_fragments": True}},
}

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

async def validate_url(url: str) -> tuple[bool, str]:
    try:
        if len(url) > 2048:
            return False, "❌ Invalid URL: Bahut lamba hai."
        parsed = urlparse(url)
        if parsed.scheme not in ["http", "https"]:
            return False, "❌ Sirf http/https URLs allowed hain."
        if not parsed.hostname:
            return False, "❌ Invalid hostname."
        lower_host = parsed.hostname.lower()
        if any(x in lower_host for x in ["localhost", "127.0.0.1", "0.0.0.0"]):
            return False, "❌ Localhost URLs not allowed."
        try:
            ip = ipaddress.ip_address(parsed.hostname)
            if not ip.is_global:
                return False, "❌ Private/unsafe IP not allowed."
        except ValueError:
            pass
        loop = asyncio.get_running_loop()
        addrs = await loop.getaddrinfo(parsed.hostname, None)
        for addr in addrs:
            ip = ipaddress.ip_address(addr[4][0])
            if not ip.is_global:
                return False, "❌ Unsafe network address."
        return True, ""
    except Exception:
        return False, "❌ URL parse nahi ho saka."

async def download_and_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    status_msg = None
    temp_dir = None
    video_path = None
    try:
        async with download_semaphore:
            status_msg = await update.message.reply_text("🔎 URL validating...")
            valid, err = await validate_url(url)
            if not valid:
                await status_msg.edit_text(err)
                return
            await status_msg.edit_text("⬇️ Downloading best quality (max 400MB)...")
            temp_dir_obj = tempfile.TemporaryDirectory(dir="/tmp")
            temp_dir = temp_dir_obj.name
            loop = asyncio.get_running_loop()
            ydl_opts = YDL_OPTIONS.copy()
            ydl_opts["outtmpl"] = os.path.join(temp_dir, ydl_opts["outtmpl"])
            def run_ydl():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return ydl.prepare_filename(info), info
            video_path, info = await loop.run_in_executor(None, run_ydl)
            if not os.path.exists(video_path):
                await status_msg.edit_text("❌ Video file not found after download.")
                return
            file_size = os.path.getsize(video_path)
            if file_size > MAX_FILE_SIZE:
                await status_msg.edit_text("❌ Video 400 MB se bada hai.")
                return
            await status_msg.edit_text("⬆️ Uploading to Telegram...")
            title = (info.get("title") or "Video")[:800]
            size_mb = file_size / (1024 * 1024)
            caption = f"✅ {title}\n📦 Size: {size_mb:.2f} MB\n⏱️ Auto-delete: 37 minutes"
            with open(video_path, "rb") as f:
                try:
                    sent = await context.bot.send_video(
                        chat_id=update.effective_chat.id,
                        video=f,
                        caption=caption,
                        supports_streaming=True,
                        read_timeout=300,
                        write_timeout=300,
                        reply_to_message_id=update.message.message_id
                    )
                except Exception:
                    sent = await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=f,
                        caption=caption,
                        read_timeout=300,
                        write_timeout=300,
                        reply_to_message_id=update.message.message_id
                    )
            asyncio.create_task(delete_after_delay(context, update.effective_chat.id, sent.message_id))
            try:
                await status_msg.delete()
            except:
                pass
    except yt_dlp.utils.DownloadError:
        msg = "❌ Download failed."
        await (status_msg.edit_text(msg) if status_msg else update.message.reply_text(msg))
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        msg = "❌ Unexpected error."
        await (status_msg.edit_text(msg) if status_msg else update.message.reply_text(msg))
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

async def delete_after_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int):
    await asyncio.sleep(AUTO_DELETE_SECONDS)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception as e:
        logger.warning(f"Delete failed: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Namaste! Video URL bhejo (max 400 MB).")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📖 Video URL bhej do.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    urls = re.findall(r'https?://\S+', text)
    for url in urls[:1]:
        await download_and_upload(update, context, url)

def main():
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN missing!")
        return
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & \~filters.COMMAND, handle_message))
    
    async def post_init(app):
        await app.bot.set_my_commands([BotCommand("start", "Start"), BotCommand("help", "Help")])
    app.post_init = post_init
    
    logger.info(f"Starting webhook on port {PORT}")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
        webhook_url=f"https://telegram-bot-3.onrender.com/{TOKEN}"
    )

if __name__ == "__main__":
    main()