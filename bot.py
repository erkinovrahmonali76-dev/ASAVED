import os
import re
import uuid
import shutil
import asyncio
import logging
import tempfile
import subprocess

import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, FSInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "asaved_bot")  # @siz ko'rsatmasdan
COOKIES = os.getenv("COOKIES_FILE")  # ixtiyoriy: cookies.txt yo'li (YouTube/Instagram uchun)
MAX_SIZE = 50 * 1024 * 1024  # Telegram bot limiti: 50 MB

bot = Bot(TOKEN)
dp = Dispatcher()

URL_RE = re.compile(r"https?://\S+")
SUPPORTED = ("instagram.com", "youtube.com", "youtu.be", "tiktok.com")

SEARCHES = {}  # sid -> [{"id","title","dur"}]
LINKS = {}     # lid -> url


def remember(store: dict, key, value):
    store[key] = value
    if len(store) > 500:
        store.pop(next(iter(store)))


def base_opts():
    o = {"quiet": True, "noplaylist": True, "no_warnings": True}
    if COOKIES and os.path.exists(COOKIES):
        o["cookiefile"] = COOKIES
    return o


def fmt_dur(sec):
    if not sec:
        return ""
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


# ---------- blocking ishlar (thread ichida) ----------
def search_music(query: str):
    opts = base_opts() | {"extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"ytsearch10:{query}", download=False)
    return [
        {"id": e["id"], "title": e.get("title") or "?", "dur": e.get("duration")}
        for e in info.get("entries", []) if e
    ]


def download_audio(url: str, folder: str):
    opts = base_opts() | {
        "format": "bestaudio/best",
        "outtmpl": f"{folder}/%(id)s.%(ext)s",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    return f"{folder}/{info['id']}.mp3", info


def download_video(url: str, folder: str):
    opts = base_opts() | {
        "format": "best[ext=mp4][height<=720]/best[height<=720]/best",
        "outtmpl": f"{folder}/%(id)s.%(ext)s",
        "merge_output_format": "mp4",
    }
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
        path = y.prepare_filename(info)
    if not os.path.exists(path):
        base = os.path.splitext(path)[0]
        path = base + ".mp4"
    return path, info


def make_round(src: str, dst: str):
    vf = "crop=min(iw\\,ih):min(iw\\,ih),scale=640:640"
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-t", "60", "-vf", vf,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", dst],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ---------- handlerlar ----------
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "👋 Salom! Men <b>ASAVED BOT</b>.\n\n"
        "📥 Instagram / YouTube / TikTok <b>havolasini</b> yuboring — videoni yuklab beraman.\n"
        "🎵 <b>Qo'shiq yoki ijrochi nomini</b> yozing — 10 ta natija chiqaraman, raqamni bossangiz mp3 olasiz.\n"
        "⭕ <b>Video yuboring</b> (galereyadan) — dumaloq video qilib beraman.",
        parse_mode="HTML",
    )


@dp.message(F.video)
async def to_round(m: Message):
    if m.video.file_size and m.video.file_size > 20 * 1024 * 1024:
        return await m.answer("⚠️ Video 20 MB dan oshmasligi kerak.")
    wait = await m.answer("⏳ Dumaloq video tayyorlanmoqda...")
    tmp = tempfile.mkdtemp()
    try:
        src, dst = f"{tmp}/in.mp4", f"{tmp}/round.mp4"
        await bot.download(m.video, destination=src)
        await asyncio.to_thread(make_round, src, dst)
        await m.answer_video_note(FSInputFile(dst), length=640)
    except Exception:
        logging.exception("round error")
        await m.answer("❌ Dumaloq video qilib bo'lmadi.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        await wait.delete()


@dp.message(F.text)
async def text_handler(m: Message):
    text = m.text.strip()
    link = URL_RE.search(text)

    if link:
        url = link.group(0)
        if not any(d in url for d in SUPPORTED):
            return await m.answer("⚠️ Faqat Instagram, YouTube va TikTok havolalari.")
        wait = await m.answer("⏳ Yuklanmoqda...")
        tmp = tempfile.mkdtemp()
        try:
            path, info = await asyncio.to_thread(download_video, url, tmp)
            if os.path.getsize(path) > MAX_SIZE:
                return await m.answer("⚠️ Video 50 MB dan katta, yuborib bo'lmaydi.")
            lid = uuid.uuid4().hex[:8]
            remember(LINKS, lid, url)
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎵 Qo'shiqni yuklab olish", callback_data=f"a:{lid}")
            ]])
            await m.answer_video(
                FSInputFile(path),
                caption=f"📩 @{BOT_USERNAME} orqali yuklab olindi",
                reply_markup=kb,
            )
        except Exception:
            logging.exception("video error")
            await m.answer("❌ Yuklab bo'lmadi. Havola yopiq yoki xato bo'lishi mumkin.")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            await wait.delete()
        return

    # musiqa qidirish
    wait = await m.answer("🔍 Qidirilmoqda...")
    try:
        results = await asyncio.to_thread(search_music, text)
    except Exception:
        logging.exception("search error")
        results = []
    await wait.delete()
    if not results:
        return await m.answer("😕 Hech narsa topilmadi.")

    sid = uuid.uuid4().hex[:8]
    remember(SEARCHES, sid, results)
    lines = [f"🔍 <i>{text}</i>\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"<b>{i}.</b> {r['title']} <b>{fmt_dur(r['dur'])}</b>")

    nums = [InlineKeyboardButton(text=str(i), callback_data=f"s:{sid}:{i-1}")
            for i in range(1, len(results) + 1)]
    rows = [nums[i:i + 5] for i in range(0, len(nums), 5)]
    rows.append([InlineKeyboardButton(text="❌", callback_data="x")])
    await m.answer("\n".join(lines), parse_mode="HTML",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def send_mp3(call: CallbackQuery, url: str):
    wait = await call.message.answer("⏳ MP3 tayyorlanmoqda...")
    tmp = tempfile.mkdtemp()
    try:
        path, info = await asyncio.to_thread(download_audio, url, tmp)
        if os.path.getsize(path) > MAX_SIZE:
            return await call.message.answer("⚠️ Fayl 50 MB dan katta.")
        await call.message.answer_audio(
            FSInputFile(path),
            title=info.get("title"),
            performer=info.get("uploader"),
            duration=info.get("duration"),
            caption=f"🎵 @{BOT_USERNAME}",
        )
    except Exception:
        logging.exception("audio error")
        await call.message.answer("❌ MP3 qilib bo'lmadi.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        await wait.delete()


@dp.callback_query(F.data.startswith("s:"))
async def pick_song(call: CallbackQuery):
    _, sid, idx = call.data.split(":")
    results = SEARCHES.get(sid)
    if not results:
        return await call.answer("Qidiruv eskirgan, qaytadan yozing.", show_alert=True)
    await call.answer("Yuklanmoqda...")
    vid = results[int(idx)]["id"]
    await send_mp3(call, f"https://www.youtube.com/watch?v={vid}")


@dp.callback_query(F.data.startswith("a:"))
async def audio_from_link(call: CallbackQuery):
    url = LINKS.get(call.data[2:])
    if not url:
        return await call.answer("Havola eskirgan.", show_alert=True)
    await call.answer("Yuklanmoqda...")
    await send_mp3(call, url)


@dp.callback_query(F.data == "x")
async def close(call: CallbackQuery):
    await call.message.delete()


# ---------- Render/Koyeb uchun health server ----------
async def health(_):
    return web.Response(text="ok")


async def main():
    port = int(os.getenv("PORT", "8000"))
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
