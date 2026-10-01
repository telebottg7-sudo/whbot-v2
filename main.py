#!/usr/bin/env python3
"""
WHBot v2 – Clean Pyrogram bot for WatchHentai
Supports:
  • Single episode URL → quality select → download → send
  • Series URL → preview + download all (with preferred quality)
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified, RPCError
from pyrogram.types import CallbackQuery, Message

from keyboards import quality_keyboard, series_keyboard
from providers import WatchHentai, ProviderError
from utils.gofile import GofileUploader

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0") or 0)
ALLOWED_USERS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_USERS", "").split(",")
    if x.strip().isdigit()
}

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

PREFERRED_QUALITY = os.getenv("PREFERRED_QUALITY", "1080p")
MAX_CONCURRENT = max(int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")), 1)
TELEGRAM_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024  # 2 GB

if not all([API_ID, API_HASH, BOT_TOKEN]):
    raise SystemExit("API_ID, API_HASH and BOT_TOKEN are required in .env")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("whbot")

# ---------------------------------------------------------------------------
# Global state (simple in-memory for single-instance bot)
# ---------------------------------------------------------------------------
# callback_data → temporary payload
PENDING: dict[str, Any] = {}
# user_id → currently running task
ACTIVE_TASKS: dict[int, asyncio.Task] = {}

provider = WatchHentai()
gofile = GofileUploader()

app = Client(
    "whbot-v2",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(Path("./.pyrogram")),
    max_concurrent_transmissions=4,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS or user_id == BOT_OWNER_ID


def is_watchhentai_url(text: str) -> bool:
    return bool(re.search(r"https?://(?:www\.)?watchhentai\.net/", text, re.I))


def is_series_url(url: str) -> bool:
    return bool(re.search(r"watchhentai\.net/series/[^/\s]+", url, re.I))


def is_episode_url(url: str) -> bool:
    return bool(re.search(r"watchhentai\.net/videos/[^/\s]+", url, re.I))


def extract_url(text: str) -> str | None:
    m = re.search(r"https?://[^\s<>\"']+", text)
    return m.group(0).rstrip(").,]") if m else None


def choose_source(sources: list[dict], preferred: str = PREFERRED_QUALITY) -> dict | None:
    if not sources:
        return None
    # Exact match first
    for s in sources:
        if preferred.lower() in s.get("label", "").lower():
            return s
    # Priority order
    for quality in ("2160p", "1440p", "1080p", "720p", "480p"):
        for s in sources:
            if quality.lower() in s.get("label", "").lower():
                return s
    return sources[0]


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def progress_bar(current: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "░" * width
    filled = int(width * current / total)
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# Download + send pipeline
# ---------------------------------------------------------------------------
async def download_and_send(
    message: Message,
    page_url: str,
    quality_label: str | None = None,
    status_msg: Message | None = None,
) -> None:
    """Resolve → download → upload one episode."""
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0

    status = status_msg or await message.reply_text("🔍 Resolving…", quote=True)

    try:
        ep = await asyncio.to_thread(provider.get_episode, page_url, True)
    except ProviderError as e:
        await status.edit_text(f"❌ Resolver error:\n<code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)
        return

    sources = ep.get("sources") or []
    if not sources:
        await status.edit_text("❌ No playable sources found on this page.")
        return

    source = None
    if quality_label:
        for s in sources:
            if s.get("label") == quality_label:
                source = s
                break
    if not source:
        source = choose_source(sources)

    title = ep.get("title") or "video"
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", title)[:120]
    out_path = DOWNLOAD_DIR / f"{user_id}_{int(time.time())}_{safe_name}.mp4"

    last_edit = 0.0

    def on_progress(current: int, total: int, started: float):
        nonlocal last_edit
        now = time.monotonic()
        if now - last_edit < 3.5 and current < total:
            return
        last_edit = now
        speed = current / max(now - started, 0.1)
        pct = (current / total * 100) if total else 0
        bar = progress_bar(current, total)
        text = (
            f"⬇️ <b>Downloading</b>\n"
            f"<code>{html.escape(title[:60])}</code>\n\n"
            f"{bar} {pct:.1f}%\n"
            f"{human_size(current)} / {human_size(total) if total else '?'}\n"
            f"Speed: {human_size(int(speed))}/s"
        )
        try:
            asyncio.get_event_loop().create_task(
                status.edit_text(text, parse_mode=ParseMode.HTML)
            )
        except Exception:
            pass

    try:
        await status.edit_text(
            f"⬇️ Downloading <b>{html.escape(source['label'])}</b>…\n"
            f"<code>{html.escape(title[:80])}</code>",
            parse_mode=ParseMode.HTML,
        )
        await asyncio.to_thread(
            provider.download,
            source["url"],
            out_path,
            on_progress,
            page_url,
        )
    except ProviderError as e:
        await status.edit_text(f"❌ Download failed:\n<code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)
        out_path.unlink(missing_ok=True)
        return

    file_size = out_path.stat().st_size
    await status.edit_text(
        f"📤 Uploading ({human_size(file_size)})…\n<code>{html.escape(title[:80])}</code>",
        parse_mode=ParseMode.HTML,
    )

    try:
        if file_size > TELEGRAM_UPLOAD_LIMIT:
            # GoFile fallback
            result = await asyncio.to_thread(gofile.upload, out_path)
            link = result.get("downloadPage") or result.get("download_page")
            await status.edit_text(
                f"✅ <b>Uploaded to GoFile</b> (file too large for Telegram)\n\n"
                f"<b>{html.escape(title)}</b>\n"
                f"🔗 {link}",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
        else:
            caption = f"<b>{html.escape(title)}</b>\nQuality: {source['label']}"
            await message.reply_video(
                video=str(out_path),
                caption=caption,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
                quote=True,
            )
            await status.delete()
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await message.reply_video(
            video=str(out_path),
            caption=f"<b>{html.escape(title)}</b>",
            parse_mode=ParseMode.HTML,
            supports_streaming=True,
        )
        await status.delete()
    except Exception as e:
        log.exception("Upload error")
        await status.edit_text(f"❌ Upload failed:\n<code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)
    finally:
        out_path.unlink(missing_ok=True)


async def download_series(
    message: Message,
    series_url: str,
    status_msg: Message,
) -> None:
    """Download every episode of a series using preferred quality."""
    try:
        series = await asyncio.to_thread(provider.get_series, series_url)
        episodes = await asyncio.to_thread(provider.series_episodes, series_url)
    except ProviderError as e:
        await status_msg.edit_text(f"❌ {html.escape(str(e))}", parse_mode=ParseMode.HTML)
        return

    if not episodes:
        await status_msg.edit_text("❌ No episodes found on this series page.")
        return

    name = series.get("name") or "Series"
    total = len(episodes)
    await status_msg.edit_text(
        f"📺 <b>{html.escape(name)}</b>\n"
        f"Found <b>{total}</b> episodes.\n"
        f"Starting download with preferred quality: <code>{PREFERRED_QUALITY}</code>…",
        parse_mode=ParseMode.HTML,
    )

    success = 0
    for idx, item in enumerate(episodes, 1):
        page_url = item["page_url"]
        try:
            await status_msg.edit_text(
                f"📺 <b>{html.escape(name)}</b>\n"
                f"Episode {idx}/{total}\n"
                f"<code>{html.escape(page_url.split('/')[-1])}</code>",
                parse_mode=ParseMode.HTML,
            )
            # Create a temporary message context for each episode
            await download_and_send(message, page_url, status_msg=status_msg)
            success += 1
        except Exception as e:
            log.exception("Series episode failed: %s", page_url)
            await message.reply_text(
                f"⚠️ Failed episode {idx}: <code>{html.escape(str(e)[:200])}</code>",
                parse_mode=ParseMode.HTML,
            )

    await status_msg.edit_text(
        f"✅ Series finished.\n"
        f"<b>{html.escape(name)}</b>\n"
        f"Successfully sent: {success}/{total}",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
@app.on_message(filters.command(["start", "help"]))
async def cmd_start(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return await message.reply_text("⛔ You are not allowed to use this bot.")

    text = (
        "👋 <b>WHBot v2</b>\n\n"
        "Send me a <b>WatchHentai</b> link:\n"
        "• Episode → quality selection → download\n"
        "• Series → preview + download all\n\n"
        "<b>Examples</b>\n"
        "<code>https://watchhentai.net/videos/some-episode/</code>\n"
        "<code>https://watchhentai.net/series/some-series/</code>\n\n"
        "Preferred quality: <code>{}</code>".format(PREFERRED_QUALITY)
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@app.on_message(filters.text & filters.private & ~filters.command(["start", "help"]))
async def on_url(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return

    url = extract_url(message.text or "")
    if not url or not is_watchhentai_url(url):
        return await message.reply_text(
            "Please send a valid WatchHentai episode or series URL.",
            quote=True,
        )

    # Cancel any previous task for this user
    if message.from_user.id in ACTIVE_TASKS:
        ACTIVE_TASKS[message.from_user.id].cancel()

    status = await message.reply_text("🔍 Looking up…", quote=True)

    if is_series_url(url):
        try:
            series = await asyncio.to_thread(provider.get_series, url)
            episodes = await asyncio.to_thread(provider.series_episodes, url)
        except ProviderError as e:
            return await status.edit_text(f"❌ {html.escape(str(e))}", parse_mode=ParseMode.HTML)

        name = series.get("name") or "Unknown Series"
        total = len(episodes) or series.get("total_episodes") or 0
        thumb = series.get("thumbnail")

        text = (
            f"📺 <b>{html.escape(name)}</b>\n"
            f"Episodes: <b>{total}</b>\n\n"
            f"Press the button below to download all episodes "
            f"using preferred quality (<code>{PREFERRED_QUALITY}</code>)."
        )

        # Store series data for callback
        sid = f"s{message.id}"
        PENDING[sid] = {"type": "series", "url": url, "user_id": message.from_user.id}

        kb = series_keyboard(total, sid)
        if thumb:
            try:
                await status.delete()
                await message.reply_photo(
                    photo=thumb,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    quote=True,
                )
                return
            except Exception:
                pass

        await status.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if is_episode_url(url):
        try:
            ep = await asyncio.to_thread(provider.get_episode, url, True)
        except ProviderError as e:
            return await status.edit_text(f"❌ {html.escape(str(e))}", parse_mode=ParseMode.HTML)

        sources = ep.get("sources") or []
        if not sources:
            return await status.edit_text("❌ No sources found.")

        title = ep.get("title") or "Episode"
        text = (
            f"🎬 <b>{html.escape(title)}</b>\n\n"
            f"Available qualities:\n"
            + "\n".join(f"• {s['label']}" for s in sources)
        )

        eid = f"e{message.id}"
        PENDING[eid] = {
            "type": "episode",
            "url": url,
            "sources": sources,
            "user_id": message.from_user.id,
            "title": title,
        }

        await status.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=quality_keyboard(sources, callback_prefix=f"q|{eid}"),
        )
        return

    await status.edit_text("Unsupported WatchHentai URL type.")


@app.on_callback_query()
async def on_callback(client: Client, query: CallbackQuery):
    data = query.data or ""
    user_id = query.from_user.id

    if not is_allowed(user_id):
        return await query.answer("Not allowed", show_alert=True)

    if data == "cancel":
        await query.message.edit_text("❌ Cancelled.")
        return await query.answer()

    # Quality selection: q|<eid>|<label>
    if data.startswith("q|"):
        parts = data.split("|", 2)
        if len(parts) != 3:
            return await query.answer("Invalid data", show_alert=True)
        _, eid, label = parts
        payload = PENDING.pop(eid, None)
        if not payload or payload.get("user_id") != user_id:
            return await query.answer("Session expired", show_alert=True)

        await query.answer(f"Downloading {label}…")
        await query.message.edit_reply_markup(None)

        task = asyncio.create_task(
            download_and_send(query.message, payload["url"], quality_label=label, status_msg=query.message)
        )
        ACTIVE_TASKS[user_id] = task
        try:
            await task
        finally:
            ACTIVE_TASKS.pop(user_id, None)
        return

    # Series download all: series_all|<sid>
    if data.startswith("series_all|"):
        sid = data.split("|", 1)[1]
        payload = PENDING.pop(sid, None)
        if not payload or payload.get("user_id") != user_id:
            return await query.answer("Session expired", show_alert=True)

        await query.answer("Starting series download…")
        await query.message.edit_reply_markup(None)

        task = asyncio.create_task(
            download_series(query.message, payload["url"], status_msg=query.message)
        )
        ACTIVE_TASKS[user_id] = task
        try:
            await task
        finally:
            ACTIVE_TASKS.pop(user_id, None)
        return

    await query.answer()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("Starting WHBot v2…")
    app.run()
