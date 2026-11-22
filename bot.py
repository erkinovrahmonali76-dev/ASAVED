import os
import re
import html
import uuid
import shutil
import asyncio
import logging
import tempfile
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import yt_dlp
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.filters import CommandStart, Command, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter,
)
from aiogram.types import (
    Message, CallbackQuery, FSInputFile, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
OWNERS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
COOKIES = os.getenv("COOKIES_FILE")
PROXY = os.getenv("PROXY_URL")
MAX_SIZE = 50 * 1024 * 1024

bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())
admin = Router()
main = Router()

BOT_USERNAME = os.getenv("BOT_USERNAME", "asaved_bot")
URL_RE = re.compile(r"https?://\S+")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SUPPORTED = ("instagram.com", "youtube.com", "youtu.be", "tiktok.com")
SEARCHES, LINKS = {}, {}
BG = set()
USER_MODE = set()  # test rejimi: admin o'zini oddiy foydalanuvchidek sinaydi
ADMIN_BTN = "⚙️ Boshqarish"
ADMIN_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=ADMIN_BTN)]], resize_keyboard=True)

# ====================== DATABASE / SOZLAMALAR ======================
pool = None
ADMIN_SET = set()
DEFAULTS = {
    "start_text": (
        "👋 Salom! Men <b>ASAVED BOT</b>.\n\n"
        "📥 Instagram / YouTube / TikTok <b>havolasini</b> yuboring — videoni yuklab beraman.\n"
        "🎵 <b>Qo'shiq yoki ijrochi nomini</b> yozing — 10 ta natija chiqaraman.\n"
        "⭕ <b>Video yuboring</b> — dumaloq video qilib beraman."
    ),
    "caption": "📩 @{bot} orqali yuklab olindi",
    "pro_text": "💎 Cheksiz foydalanish uchun PRO oling — admin bilan bog'laning.",
    "maint_text": "🛠 Botda texnik ishlar ketmoqda. Birozdan so'ng qaytadan urinib ko'ring.",
    "daily_limit": "20",
    "sub_enabled": "1",
    "maintenance": "0",
    "feat_video": "1",
    "feat_music": "1",
    "feat_round": "1",
}
CFG = {}
TOGGLES = {
    "sub_enabled": "Majburiy obuna",
    "maintenance": "Texnik ishlar (maintenance)",
    "feat_video": "Video yuklash",
    "feat_music": "Musiqa qidirish",
    "feat_round": "Dumaloq video",
}
TEXTS = {
    "start_text": "✏️ Start matni",
    "caption": "✏️ Video izohi (caption)",
    "pro_text": "✏️ PRO matni",
    "maint_text": "✏️ Maintenance matni",
}


def clean_dsn(u: str) -> str:
    p = urlsplit(u)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "channel_binding"]
    return urlunsplit(p._replace(query=urlencode(q)))


async def db_init():
    global pool
    pool = await asyncpg.create_pool(clean_dsn(DATABASE_URL), min_size=1, max_size=5)
    async with pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id BIGINT PRIMARY KEY, name TEXT, username TEXT,
            joined TIMESTAMPTZ DEFAULT now(),
            banned BOOLEAN DEFAULT FALSE, active BOOLEAN DEFAULT TRUE);
        CREATE TABLE IF NOT EXISTS channels(
            chat_id BIGINT PRIMARY KEY, title TEXT, link TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS admins(id BIGINT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS downloads(
            id SERIAL PRIMARY KEY, user_id BIGINT, kind TEXT,
            ts TIMESTAMPTZ DEFAULT now());
        CREATE TABLE IF NOT EXISTS admin_log(
            id SERIAL PRIMARY KEY, admin_id BIGINT, admin_name TEXT,
            action TEXT, ts TIMESTAMPTZ DEFAULT now());
        ALTER TABLE users ADD COLUMN IF NOT EXISTS pro_until TIMESTAMPTZ;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ DEFAULT now();
        ALTER TABLE downloads ADD COLUMN IF NOT EXISTS platform TEXT;
        ALTER TABLE downloads ADD COLUMN IF NOT EXISTS ok BOOLEAN DEFAULT TRUE;
        ALTER TABLE downloads ADD COLUMN IF NOT EXISTS err TEXT;
        ALTER TABLE downloads ADD COLUMN IF NOT EXISTS query TEXT;
        """)
        CFG.update(DEFAULTS)
        for r in await c.fetch("SELECT key, value FROM settings"):
            CFG[r["key"]] = r["value"]
        for r in await c.fetch("SELECT id FROM admins"):
            ADMIN_SET.add(r["id"])


async def set_cfg(key, value):
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO settings(key,value) VALUES($1,$2) "
            "ON CONFLICT(key) DO UPDATE SET value=$2", key, value)
    CFG[key] = value


def is_admin(uid: int) -> bool:
    return uid in OWNERS or uid in ADMIN_SET


def eff_admin(uid: int) -> bool:
    return is_admin(uid) and uid not in USER_MODE


async def alog(user, action: str):
    try:
        async with pool.acquire() as c:
            await c.execute(
                "INSERT INTO admin_log(admin_id, admin_name, action) VALUES($1,$2,$3)",
                user.id, user.full_name, action)
    except Exception:
        logging.exception("alog")


async def log_dl(uid, kind, platform=None, ok=True, err=None, query=None):
    try:
        async with pool.acquire() as c:
            await c.execute(
                "INSERT INTO downloads(user_id, kind, platform, ok, err, query) "
                "VALUES($1,$2,$3,$4,$5,$6)", uid, kind, platform, ok, err, query)
    except Exception:
        logging.exception("log_dl")


def e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def short_err(ex) -> str:
    return ANSI_RE.sub("", str(ex)).strip().replace("\n", " ")[:300]


def platform_of(url: str) -> str:
    for key, name in (("instagram", "instagram"), ("youtu", "youtube"), ("tiktok", "tiktok")):
        if key in url:
            return name
    return "other"


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows
    ])


def remember(store: dict, key, value):
    store[key] = value
    if len(store) > 500:
        store.pop(next(iter(store)))


# ====================== LIMIT / PRO ======================
async def over_limit(uid: int) -> bool:
    if eff_admin(uid):
        return False
    limit = int(CFG.get("daily_limit") or 0)
    if limit <= 0:
        return False
    async with pool.acquire() as c:
        pro = await c.fetchval("SELECT pro_until > now() FROM users WHERE id=$1", uid)
        if pro:
            return False
        n = await c.fetchval(
            "SELECT count(*) FROM downloads WHERE user_id=$1 AND ok "
            "AND kind IN ('video','music','round') AND ts > date_trunc('day', now())", uid)
    return n >= limit


def limit_text() -> str:
    return f"⚠️ Bugungi limit ({CFG['daily_limit']} ta) tugadi. Ertaga yangilanadi.\n\n{CFG['pro_text']}"


# ====================== MAJBURIY OBUNA ======================
async def unsubscribed(user_id: int):
    async with pool.acquire() as c:
        chans = await c.fetch("SELECT chat_id, title, link FROM channels")
    missing = []
    for ch in chans:
        try:
            m = await bot.get_chat_member(ch["chat_id"], user_id)
            if m.status in ("left", "kicked") or (m.status == "restricted" and not m.is_member):
                missing.append(ch)
        except Exception as ex:
            logging.warning("obuna tekshiruvi xato (%s): %s", ch["title"], ex)
    return missing


async def send_gate(msg: Message, missing):
    rows = [[InlineKeyboardButton(text=f"📢 {ch['title']}", url=ch["link"])] for ch in missing]
    rows.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="chk")])
    await msg.answer(
        "❗ Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling, so'ng «Tekshirish» ni bosing:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


class Gate(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        msg = event if isinstance(event, Message) else event.message
        if not user or user.is_bot or msg is None or msg.chat.type != "private":
            return await handler(event, data)

        async with pool.acquire() as c:
            banned = await c.fetchval(
                "INSERT INTO users(id,name,username,last_seen) VALUES($1,$2,$3,now()) "
                "ON CONFLICT(id) DO UPDATE SET name=$2, username=$3, active=TRUE, last_seen=now() "
                "RETURNING banned", user.id, user.full_name, user.username)

        if is_admin(user.id):
            cb = event.data if isinstance(event, CallbackQuery) else ""
            txt = event.text if isinstance(event, Message) else None
            if (not user.id in USER_MODE or data.get("raw_state") or cb.startswith("ad:")
                    or txt in (ADMIN_BTN, "/admin", "/cancel")):
                return await handler(event, data)

        async def deny(text):
            if isinstance(event, CallbackQuery):
                await event.answer(text[:190], show_alert=True)
            else:
                await event.answer(text, parse_mode="HTML")

        if banned:
            return await deny("🚫 Siz bloklangansiz.")
        if CFG.get("maintenance") == "1":
            return await deny(CFG["maint_text"])

        is_chk = isinstance(event, CallbackQuery) and event.data == "chk"
        if CFG.get("sub_enabled") == "1" and not is_chk:
            missing = await unsubscribed(user.id)
            if missing:
                await send_gate(msg, missing)
                if isinstance(event, CallbackQuery):
                    await event.answer()
                return
        return await handler(event, data)


dp.message.outer_middleware(Gate())
dp.callback_query.outer_middleware(Gate())


# ====================== YUKLASH FUNKSIYALARI ======================
def base_opts():
    o = {"quiet": True, "noplaylist": True, "no_warnings": True, "socket_timeout": 20}
    if COOKIES and os.path.exists(COOKIES):
        local = "/tmp/cookies.txt"
        if not os.path.exists(local):
            shutil.copy(COOKIES, local)
        o["cookiefile"] = local
    if PROXY:
        o["proxy"] = PROXY
    return o


def fmt_dur(sec):
    if not sec:
        return ""
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


def search_music(query: str):
    opts = base_opts() | {"extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"ytsearch10:{query}", download=False)
    return [
        {"id": x["id"], "title": x.get("title") or "?", "dur": x.get("duration")}
        for x in info.get("entries", []) if x
    ]


YT_ATTEMPTS = [
    None,
    {"player_client": ["tv", "web_safari"]},
    {"player_client": ["ios"]},
    {"player_client": ["android_vr"]},
    {"player_client": ["mweb"]},
]


def _download_audio(url: str, folder: str, yt_args=None):
    opts = base_opts() | {
        "format": "bestaudio/best",
        "outtmpl": f"{folder}/%(id)s.%(ext)s",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    if yt_args:
        opts["extractor_args"] = {"youtube": yt_args}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    if info.get("entries"):
        info = info["entries"][0]
    path = f"{folder}/{info['id']}.mp3"
    if not os.path.exists(path):
        # fayl nomi boshqacha chiqsa (maxsus belgilar), papkadan mp3 ni topamiz
        found = sorted(
            (os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".mp3")),
            key=os.path.getmtime, reverse=True)
        if not found:
            raise RuntimeError("MP3 fayl yaratilmadi (ffmpeg o'rnatilganmi?)")
        path = found[0]
    return path, info


def title_variants(title: str):
    t = re.sub(r"[\(\[].*?[\)\]]", " ", title or "")
    t = re.sub(r"\s+", " ", t).strip()
    out = []
    for v in (t, " ".join(t.split()[:6])):
        if v and v not in out:
            out.append(v)
    return out


def sc_candidates(q: str, dur=None):
    opts = base_opts() | {"extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"scsearch8:{q}", download=False)
    items = [x for x in info.get("entries", []) if x]

    def key(x):
        d = x.get("duration")
        return abs(d - dur) if (d and dur) else 9999

    items.sort(key=key)
    urls = []
    for x in items:
        u = x.get("webpage_url") or x.get("url")
        d = x.get("duration")
        if not u:
            continue
        # davomiyligi mos kelmaydigan treklarni yuklamasdan oldin o'tkazib yuboramiz
        if dur and d and abs(d - dur) > max(25, dur * 0.15):
            continue
        urls.append(u)
    return urls[:6]


def download_audio(url: str, folder: str, fallback: str = None, dur=None):
    yt_errs, sc_err = [], None
    is_yt = "youtube.com" in url or "youtu.be" in url
    for args in (YT_ATTEMPTS if is_yt else [None]):
        try:
            return _download_audio(url, folder, args)
        except Exception as ex:
            yt_errs.append(short_err(ex))
            logging.warning("audio urinish xato (%s): %s", args, short_err(ex))
    if fallback:
        seen, tries = set(), 0
        for q in title_variants(fallback):
            try:
                urls = sc_candidates(q, dur)
            except Exception as ex:
                sc_err = short_err(ex)
                logging.warning("SoundCloud qidiruv xato (%s): %s", q, short_err(ex))
                continue
            if not urls:
                sc_err = "mos trek topilmadi"
            for u in urls:
                if u in seen or tries >= 8:
                    continue
                seen.add(u)
                tries += 1
                try:
                    path, info = _download_audio(u, folder)
                    d = info.get("duration")
                    if dur and d and abs(d - dur) > max(25, dur * 0.15):
                        sc_err = f"davomiylik mos emas ({d}s, kutilgan {dur}s)"
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                        continue
                    return path, info
                except Exception as ex:
                    sc_err = short_err(ex)
                    logging.warning("SoundCloud xato (%s): %s", u, short_err(ex))
    # Asosiy sabab (birinchi YouTube xatosi) ko'rinib tursin
    first = yt_errs[0] if yt_errs else "noma'lum xato"
    msg = f"{'YouTube' if is_yt else 'Yuklash'}: {first[:230]}"
    if sc_err:
        msg += f" | SoundCloud: {sc_err[:50]}"
    raise RuntimeError(msg)


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
        path = os.path.splitext(path)[0] + ".mp4"
    return path, info


def make_round(src: str, dst: str):
    vf = "crop=min(iw\\,ih):min(iw\\,ih),scale=640:640"
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-t", "60", "-vf", vf,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", dst],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ====================== ODDIY FOYDALANUVCHI ======================
OFF = "⛔ Bu funksiya vaqtincha o'chirilgan."


@main.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(CFG["start_text"], parse_mode="HTML",
                   reply_markup=ADMIN_KB if is_admin(m.from_user.id) else None)


@main.callback_query(F.data == "chk")
async def check_sub(call: CallbackQuery):
    missing = await unsubscribed(call.from_user.id)
    if missing:
        return await call.answer("❌ Hali hamma kanalga obuna bo'lmadingiz.", show_alert=True)
    await call.answer("✅ Rahmat!")
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(CFG["start_text"], parse_mode="HTML")


@main.message(F.video)
async def to_round(m: Message):
    uid = m.from_user.id
    if CFG["feat_round"] != "1" and not eff_admin(uid):
        return await m.answer(OFF)
    if await over_limit(uid):
        return await m.answer(limit_text(), parse_mode="HTML")
    if m.video.file_size and m.video.file_size > 20 * 1024 * 1024:
        return await m.answer("⚠️ Video 20 MB dan oshmasligi kerak.")
    wait = await m.answer("⏳ Dumaloq video tayyorlanmoqda...")
    tmp = tempfile.mkdtemp()
    try:
        src, dst = f"{tmp}/in.mp4", f"{tmp}/round.mp4"
        await bot.download(m.video, destination=src)
        await asyncio.to_thread(make_round, src, dst)
        await m.answer_video_note(FSInputFile(dst), length=640)
        await log_dl(uid, "round", "telegram")
    except Exception as ex:
        logging.exception("round error")
        await log_dl(uid, "round", "telegram", ok=False, err=short_err(ex))
        await m.answer("❌ Dumaloq video qilib bo'lmadi.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        await wait.delete()


@main.message(F.text, ~F.text.startswith("/"))
async def text_handler(m: Message):
    uid = m.from_user.id
    text = m.text.strip()
    link = URL_RE.search(text)

    if link:
        if CFG["feat_video"] != "1" and not eff_admin(uid):
            return await m.answer(OFF)
        url = link.group(0)
        if not any(d in url for d in SUPPORTED):
            return await m.answer("⚠️ Faqat Instagram, YouTube va TikTok havolalari.")
        if await over_limit(uid):
            return await m.answer(limit_text(), parse_mode="HTML")
        wait = await m.answer("⏳ Yuklanmoqda...")
        tmp = tempfile.mkdtemp()
        try:
            path, info = await asyncio.to_thread(download_video, url, tmp)
            if os.path.getsize(path) > MAX_SIZE:
                return await m.answer("⚠️ Video 50 MB dan katta, yuborib bo'lmaydi.")
            lid = uuid.uuid4().hex[:8]
            remember(LINKS, lid, url)
            await m.answer_video(
                FSInputFile(path),
                caption=CFG["caption"].replace("{bot}", BOT_USERNAME),
                reply_markup=kb([[("🎵 Qo'shiqni yuklab olish", f"a:{lid}")]]),
            )
            await log_dl(uid, "video", platform_of(url))
        except Exception as ex:
            logging.exception("video error")
            await log_dl(uid, "video", platform_of(url), ok=False, err=short_err(ex))
            msg = "❌ Yuklab bo'lmadi. Havola yopiq yoki xato bo'lishi mumkin."
            if is_admin(uid):
                msg += f"\n\n<code>{e(short_err(ex))}</code>"
            await m.answer(msg, parse_mode="HTML")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            await wait.delete()
        return

    if CFG["feat_music"] != "1" and not eff_admin(uid):
        return await m.answer(OFF)
    wait = await m.answer("🔍 Qidirilmoqda...")
    try:
        results = await asyncio.to_thread(search_music, text)
    except Exception as ex:
        logging.exception("search error")
        await log_dl(uid, "search", "youtube", ok=False, err=short_err(ex), query=text[:200])
        results = []
    await wait.delete()
    if not results:
        return await m.answer("😕 Hech narsa topilmadi.")
    await log_dl(uid, "search", "youtube", query=text[:200])

    sid = uuid.uuid4().hex[:8]
    remember(SEARCHES, sid, results)
    lines = [f"🔍 <i>{e(text)}</i>\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"<b>{i}.</b> {e(r['title'])} <b>{fmt_dur(r['dur'])}</b>")
    nums = [InlineKeyboardButton(text=str(i), callback_data=f"s:{sid}:{i-1}")
            for i in range(1, len(results) + 1)]
    rows = [nums[i:i + 5] for i in range(0, len(nums), 5)]
    rows.append([InlineKeyboardButton(text="❌", callback_data="x")])
    await m.answer("\n".join(lines), parse_mode="HTML",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def send_mp3(call: CallbackQuery, url: str, fallback: str = None, dur=None):
    uid = call.from_user.id
    wait = await call.message.answer("⏳ MP3 tayyorlanmoqda...")
    tmp = tempfile.mkdtemp()
    try:
        path, info = await asyncio.to_thread(download_audio, url, tmp, fallback, dur)
        if os.path.getsize(path) > MAX_SIZE:
            return await call.message.answer("⚠️ Fayl 50 MB dan katta.")
        await call.message.answer_audio(
            FSInputFile(path),
            title=info.get("title"),
            performer=info.get("uploader"),
            duration=int(round(float(info.get("duration") or 0))) or None,
            caption=f"🎵 @{BOT_USERNAME}",
        )
        src = (info.get("extractor_key") or platform_of(url)).lower()
        await log_dl(uid, "music", src, query=(fallback or info.get("title") or "")[:200])
    except Exception as ex:
        logging.exception("audio error")
        await log_dl(uid, "music", platform_of(url), ok=False, err=short_err(ex),
                     query=(fallback or "")[:200])
        msg = "❌ MP3 qilib bo'lmadi. Keyinroq urinib ko'ring."
        if is_admin(uid):
            msg += f"\n\n<code>{e(short_err(ex))}</code>"
        await call.message.answer(msg, parse_mode="HTML")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        await wait.delete()


@main.callback_query(F.data.startswith("s:"))
async def pick_song(call: CallbackQuery):
    _, sid, idx = call.data.split(":")
    results = SEARCHES.get(sid)
    if not results:
        return await call.answer("Qidiruv eskirgan, qaytadan yozing.", show_alert=True)
    if await over_limit(call.from_user.id):
        await call.answer()
        return await call.message.answer(limit_text(), parse_mode="HTML")
    await call.answer("Yuklanmoqda...")
    song = results[int(idx)]
    await send_mp3(call, f"https://www.youtube.com/watch?v={song['id']}", song["title"], song["dur"])


@main.callback_query(F.data.startswith("a:"))
async def audio_from_link(call: CallbackQuery):
    url = LINKS.get(call.data[2:])
    if not url:
        return await call.answer("Havola eskirgan.", show_alert=True)
    if await over_limit(call.from_user.id):
        await call.answer()
        return await call.message.answer(limit_text(), parse_mode="HTML")
    await call.answer("Yuklanmoqda...")
    await send_mp3(call, url)


@main.callback_query(F.data == "x")
async def close(call: CallbackQuery):
    await call.message.delete()


# ====================== ADMIN PANEL ======================
class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        return bool(event.from_user) and is_admin(event.from_user.id)


admin.message.filter(IsAdmin())
admin.callback_query.filter(IsAdmin())


class S(StatesGroup):
    add_channel = State()
    edit_text = State()
    broadcast = State()
    find_user = State()
    add_admin = State()


def menu(uid: int):
    mt = "✅ YOQILGAN" if CFG["maintenance"] == "1" else "❌ o'chiq"
    ts = "✅ YOQILGAN" if uid in USER_MODE else "❌ o'chiq"
    return kb([
        [("📊 LIVE statistika", "ad:stats"), ("🔍 User qidirish", "ad:find")],
        [("📈 Kunlik grafik", "ad:chart:7"), ("🔥 Top userlar", "ad:top")],
        [("🎵 Musiqa statistika", "ad:mstat"), ("📥 Download log", "ad:log:all")],
        [("💎 PRO boshqaruvi", "ad:pro"), ("🔗 Majburiy obuna", "ad:ch")],
        [("📢 Broadcast", "ad:bc"), ("⚙️ Sozlamalar / Limit", "ad:set")],
        [(f"🛠 Maintenance: {mt}", "ad:tg:maintenance:home")],
        [(f"🧪 Test rejimi: {ts}", "ad:test")],
        [("👥 Userlar ro'yxati", "ad:users"), ("📝 Admin log", "ad:alog")],
        [("👮 Adminlar", "ad:adm")],
    ])


PANEL_TITLE = "🛠 <b>Admin panel</b>"
BACK = [("⬅️ Orqaga", "ad:home")]


async def show(call: CallbackQuery, text: str, markup):
    try:
        await call.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await call.answer()


@admin.message(F.text == ADMIN_BTN)
@admin.message(Command("admin"))
async def admin_open(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(PANEL_TITLE, reply_markup=menu(m.from_user.id), parse_mode="HTML")


@admin.message(Command("cancel"))
async def cancel_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=menu(m.from_user.id))


@admin.callback_query(F.data == "ad:home")
async def ad_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, PANEL_TITLE, menu(call.from_user.id))


# ---- 1. LIVE statistika ----
@admin.callback_query(F.data == "ad:stats")
async def ad_stats(call: CallbackQuery):
    async with pool.acquire() as c:
        u = await c.fetchrow("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE active) AS act,
                   count(*) FILTER (WHERE banned) AS ban,
                   count(*) FILTER (WHERE pro_until > now()) AS pro,
                   count(*) FILTER (WHERE last_seen > now() - interval '5 minutes') AS online,
                   count(*) FILTER (WHERE joined > date_trunc('day', now())) AS d1,
                   count(*) FILTER (WHERE joined > now() - interval '7 days') AS d7
            FROM users""")
        t = await c.fetchrow("""
            SELECT count(*) FILTER (WHERE ok AND kind <> 'search') AS total,
                   count(*) FILTER (WHERE ok AND kind <> 'search' AND ts > date_trunc('day', now())) AS today,
                   count(*) FILTER (WHERE NOT ok AND ts > date_trunc('day', now())) AS errs
            FROM downloads""")
        d = await c.fetch("""
            SELECT kind, count(*) AS total,
                   count(*) FILTER (WHERE ts > date_trunc('day', now())) AS d1
            FROM downloads WHERE ok AND kind <> 'search' GROUP BY kind""")
        nch = await c.fetchval("SELECT count(*) FROM channels")
    names = {"video": "🎬 Video", "music": "🎵 Musiqa", "round": "⭕ Dumaloq"}
    dl = "\n".join(f"{names.get(r['kind'], r['kind'])}: {r['total']} (bugun {r['d1']})" for r in d) or "—"
    now = datetime.now(timezone.utc)
    text = (
        f"📊 <b>LIVE statistika</b> <i>{now:%H:%M:%S} UTC</i>\n\n"
        f"🟢 Hozir online (5 daqiqa): <b>{u['online']}</b>\n"
        f"👥 Jami userlar: <b>{u['total']}</b>  (faol {u['act']})\n"
        f"🆕 Bugun: {u['d1']}   Hafta: {u['d7']}\n"
        f"💎 PRO: {u['pro']}   🚫 Ban: {u['ban']}\n"
        f"📢 Majburiy kanallar: {nch}\n\n"
        f"📥 Bugungi yuklamalar: <b>{t['today']}</b>\n"
        f"📦 Jami yuklamalar: <b>{t['total']}</b>\n"
        f"⚠️ Bugungi xatolar: {t['errs']}\n\n"
        f"<b>Turlar bo'yicha:</b>\n{dl}"
    )
    await show(call, text, kb([[("🔄 Yangilash", "ad:stats")], BACK]))


# ---- 7. Kunlik grafik ----
@admin.callback_query(F.data.startswith("ad:chart:"))
async def ad_chart(call: CallbackQuery):
    days = int(call.data.split(":")[2])
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT d::date AS day,
              (SELECT count(*) FROM downloads x WHERE x.ts::date = d::date AND x.ok AND x.kind <> 'search') AS dl,
              (SELECT count(*) FROM users u WHERE u.joined::date = d::date) AS nu,
              (SELECT count(DISTINCT x.user_id) FROM downloads x WHERE x.ts::date = d::date) AS au
            FROM generate_series((current_date - ($1::int - 1))::timestamp,
                                 current_date::timestamp, interval '1 day') d
            ORDER BY 1""", days)
    mx = max([r["dl"] for r in rows] + [1])
    lines = []
    for r in rows:
        bar = "█" * round(r["dl"] / mx * 10)
        lines.append(f"{r['day']:%d.%m} {bar:<10} {r['dl']:>4} yuk | +{r['nu']} yangi | {r['au']} faol")
    text = f"📈 <b>Oxirgi {days} kun</b>\n\n<pre>{e(chr(10).join(lines))}</pre>"
    await show(call, text, kb([[("7 kun", "ad:chart:7"), ("30 kun", "ad:chart:30")], BACK]))


# ---- 8. Top foydalanuvchilar ----
@admin.callback_query(F.data == "ad:top")
async def ad_top(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT u.id, u.name, u.username, count(*) AS n
            FROM downloads d JOIN users u ON u.id = d.user_id
            WHERE d.ok AND d.kind IN ('video','music','round')
            GROUP BY u.id, u.name, u.username ORDER BY n DESC LIMIT 10""")
    text = "🔥 <b>Top foydalanuvchilar</b>\n\n"
    text += "\n".join(
        f"{i}. {e(r['name'])} {'@' + e(r['username']) if r['username'] else ''} — <b>{r['n']}</b> "
        f"(<code>{r['id']}</code>)" for i, r in enumerate(rows, 1)) or "Hali ma'lumot yo'q."
    btns = [[(f"{i}. {r['name'][:25]}", f"ad:u:{r['id']}")] for i, r in enumerate(rows[:5], 1)]
    await show(call, text, kb(btns + [BACK]))


# ---- 9. Musiqa statistika ----
@admin.callback_query(F.data == "ad:mstat")
async def ad_mstat(call: CallbackQuery):
    async with pool.acquire() as c:
        songs = await c.fetch("""
            SELECT query, count(*) AS n FROM downloads
            WHERE kind='music' AND ok AND query <> '' GROUP BY query ORDER BY n DESC LIMIT 10""")
        qs = await c.fetch("""
            SELECT lower(query) AS q, count(*) AS n FROM downloads
            WHERE kind='search' AND ok AND query <> '' GROUP BY 1 ORDER BY n DESC LIMIT 10""")
    text = "🎵 <b>Eng ko'p yuklangan qo'shiqlar</b>\n"
    text += "\n".join(f"{i}. {e(r['query'])} — {r['n']}" for i, r in enumerate(songs, 1)) or "—"
    text += "\n\n🔍 <b>Eng ko'p qidirilgan so'zlar</b>\n"
    text += "\n".join(f"{i}. {e(r['q'])} — {r['n']}" for i, r in enumerate(qs, 1)) or "—"
    await show(call, text, kb([BACK]))


# ---- 10. Download log ----
@admin.callback_query(F.data.startswith("ad:log:"))
async def ad_log(call: CallbackQuery):
    only_err = call.data.split(":")[2] == "err"
    sql = ("SELECT d.* FROM downloads d WHERE d.kind <> 'search'"
           + (" AND NOT d.ok" if only_err else "") + " ORDER BY d.ts DESC LIMIT 12")
    async with pool.acquire() as c:
        rows = await c.fetch(sql)
    lines = []
    for r in rows:
        line = (f"{r['ts']:%d.%m %H:%M} {'✅' if r['ok'] else '❌'} "
                f"{r['kind']}/{r['platform'] or '-'} <code>{r['user_id']}</code>")
        if r["err"]:
            line += f"\n   <code>{e(r['err'][:140])}</code>"
        lines.append(line)
    text = f"📥 <b>Download log</b> ({'faqat xatolar' if only_err else 'hammasi'})\n\n"
    text += "\n".join(lines) or "Bo'sh."
    await show(call, text, kb([[("Hammasi", "ad:log:all"), ("Faqat xatolar", "ad:log:err")], BACK]))


# ---- foydalanuvchi kartasi / qidirish / ban / PRO ----
async def user_card(uid: int):
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT * FROM users WHERE id=$1", uid)
        cnt = await c.fetch(
            "SELECT kind, count(*) AS n FROM downloads WHERE user_id=$1 AND ok "
            "AND kind IN ('video','music','round') GROUP BY kind", uid)
    if not r:
        return None
    k = {x["kind"]: x["n"] for x in cnt}
    now = datetime.now(timezone.utc)
    pro = r["pro_until"] and r["pro_until"] > now
    text = (
        f"👤 <b>{e(r['name'])}</b>\n"
        f"ID: <code>{r['id']}</code>\n"
        f"Username: {'@' + e(r['username']) if r['username'] else '—'}\n"
        f"Qo'shilgan: {r['joined']:%Y-%m-%d %H:%M}\n"
        f"Oxirgi faollik: {r['last_seen']:%Y-%m-%d %H:%M}\n"
        f"🎬 Video: {k.get('video', 0)}   🎵 Musiqa: {k.get('music', 0)}   ⭕ Dumaloq: {k.get('round', 0)}\n"
        f"Holat: {'🚫 bloklangan' if r['banned'] else '✅ faol'}\n"
        f"💎 PRO: {('✅ ' + r['pro_until'].strftime('%Y-%m-%d') + ' gacha') if pro else '—'}"
    )
    ban = ("✅ Unban", f"ad:unban:{uid}") if r["banned"] else ("🚫 Ban", f"ad:ban:{uid}")
    return text, kb([
        [ban],
        [("💎 +7 kun", f"ad:pro:{uid}:7"), ("💎 +30 kun", f"ad:pro:{uid}:30"), ("💎 +90 kun", f"ad:pro:{uid}:90")],
        [("❌ PRO bekor qilish", f"ad:pro:{uid}:0")],
        [("⬅️ Orqaga", "ad:home")],
    ])


@admin.callback_query(F.data.startswith("ad:u:"))
async def ad_user(call: CallbackQuery):
    res = await user_card(int(call.data.split(":")[2]))
    if not res:
        return await call.answer("Topilmadi.", show_alert=True)
    await show(call, *res)


@admin.callback_query(F.data == "ad:find")
async def ad_find(call: CallbackQuery, state: FSMContext):
    await state.set_state(S.find_user)
    await call.message.answer("🔍 Foydalanuvchi <b>ID</b>, <b>@username</b> yoki <b>ism</b> yuboring. /cancel — bekor.",
                              parse_mode="HTML")
    await call.answer()


@admin.message(S.find_user, F.text)
async def find_user(m: Message, state: FSMContext):
    q = m.text.strip().lstrip("@")
    async with pool.acquire() as c:
        if q.isdigit():
            rows = await c.fetch("SELECT id, name FROM users WHERE id=$1", int(q))
        else:
            rows = await c.fetch(
                "SELECT id, name FROM users WHERE lower(username)=lower($1) OR name ILIKE $2 LIMIT 8",
                q, f"%{q}%")
    if not rows:
        return await m.answer("😕 Topilmadi. Boshqa so'z yuboring yoki /cancel.")
    await state.clear()
    if len(rows) == 1:
        text, markup = await user_card(rows[0]["id"])
        return await m.answer(text, parse_mode="HTML", reply_markup=markup)
    await m.answer("Bir nechta topildi:", reply_markup=kb(
        [[(f"{r['name'][:30]} ({r['id']})", f"ad:u:{r['id']}")] for r in rows] + [BACK]))


@admin.callback_query(F.data.startswith("ad:ban:") | F.data.startswith("ad:unban:"))
async def ad_ban(call: CallbackQuery):
    _, act, uid = call.data.split(":")
    uid = int(uid)
    if is_admin(uid):
        return await call.answer("Adminni bloklab bo'lmaydi.", show_alert=True)
    async with pool.acquire() as c:
        await c.execute("UPDATE users SET banned=$2 WHERE id=$1", uid, act == "ban")
    await alog(call.from_user, f"{act.upper()} user {uid}")
    await show(call, *(await user_card(uid)))


@admin.callback_query(F.data.startswith("ad:pro:") & (F.data != "ad:pro"))
async def ad_pro_set(call: CallbackQuery):
    _, _, uid, days = call.data.split(":")
    uid, days = int(uid), int(days)
    async with pool.acquire() as c:
        if days == 0:
            await c.execute("UPDATE users SET pro_until=NULL WHERE id=$1", uid)
        else:
            await c.execute(
                "UPDATE users SET pro_until = GREATEST(now(), COALESCE(pro_until, now())) "
                "+ make_interval(days => $2) WHERE id=$1", uid, days)
    await alog(call.from_user, f"PRO {'bekor' if days == 0 else '+' + str(days) + ' kun'} user {uid}")
    try:
        if days:
            await bot.send_message(uid, f"💎 Sizga PRO berildi (+{days} kun). Cheksiz foydalaning!")
    except Exception:
        pass
    await show(call, *(await user_card(uid)))


@admin.callback_query(F.data == "ad:pro")
async def ad_pro(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id, name, pro_until FROM users WHERE pro_until > now() ORDER BY pro_until LIMIT 15")
    text = f"💎 <b>PRO foydalanuvchilar</b>\n\nFaol PRO: {len(rows)}\n\nYangi PRO berish: «🔍 User qidirish» → userni toping → 💎 tugmalari."
    btns = [[(f"💎 {r['name'][:22]} — {r['pro_until']:%d.%m}", f"ad:u:{r['id']}")] for r in rows]
    await show(call, text, kb(btns + [[("🔍 User topib PRO berish", "ad:find")], BACK]))


# ---- userlar ro'yxati ----
@admin.callback_query(F.data == "ad:users")
async def ad_users(call: CallbackQuery, state: FSMContext):
    await state.clear()
    async with pool.acquire() as c:
        total = await c.fetchval("SELECT count(*) FROM users")
        last = await c.fetch("SELECT id, name FROM users ORDER BY joined DESC LIMIT 10")
    text = f"👥 <b>Foydalanuvchilar: {total}</b>\n\nOxirgi 10 ta:\n"
    text += "\n".join(f"• {e(r['name'])} — <code>{r['id']}</code>" for r in last)
    await show(call, text, kb([
        [("🔍 Qidirish", "ad:find")],
        [("📄 Ro'yxatni yuklab olish", "ad:export")],
        BACK,
    ]))


@admin.callback_query(F.data == "ad:export")
async def ad_export(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM users ORDER BY joined")
    lines = [f"{r['id']} | {r['name']} | @{r['username'] or '-'} | {r['joined']:%Y-%m-%d} | "
             f"{'banned' if r['banned'] else 'ok'}" for r in rows]
    await call.message.answer_document(BufferedInputFile("\n".join(lines).encode(), "users.txt"),
                                       caption=f"Jami: {len(rows)}")
    await call.answer()


# ---- 13. Majburiy obuna ----
async def channels_view(call: CallbackQuery):
    async with pool.acquire() as c:
        chans = await c.fetch("SELECT chat_id, title FROM channels ORDER BY title")
    on = CFG["sub_enabled"] == "1"
    state_txt = "✅ yoqilgan" if on else "❌ o'chirilgan"
    text = f"🔗 <b>Majburiy obuna</b>\nTekshiruv: {state_txt}\n\n"
    text += "\n".join(f"• {e(ch['title'])}" for ch in chans) or "Hali kanal qo'shilmagan."
    text += ("\n\n💡 Admin hisobingiz tekshiruvdan o'tkazib yuboriladi. "
             "Sinash uchun menyudan «🧪 Test rejimi» ni yoqing yoki boshqa akkauntdan kiring.")
    rows = [[(f"🗑 {ch['title'][:30]}", f"ad:chdel:{ch['chat_id']}")] for ch in chans]
    rows.append([("➕ Kanal qo'shish", "ad:chadd")])
    rows.append([("🔁 Obunani yoqish/o'chirish", "ad:tg:sub_enabled:ch")])
    rows.append(BACK)
    await show(call, text, kb(rows))


@admin.callback_query(F.data == "ad:ch")
async def ad_ch(call: CallbackQuery):
    await channels_view(call)


@admin.callback_query(F.data.startswith("ad:chdel:"))
async def ad_chdel(call: CallbackQuery):
    cid = int(call.data.split(":")[2])
    async with pool.acquire() as c:
        title = await c.fetchval("SELECT title FROM channels WHERE chat_id=$1", cid)
        await c.execute("DELETE FROM channels WHERE chat_id=$1", cid)
    await alog(call.from_user, f"Kanal o'chirildi: {title}")
    await channels_view(call)


@admin.callback_query(F.data == "ad:chadd")
async def ad_chadd(call: CallbackQuery, state: FSMContext):
    await state.set_state(S.add_channel)
    await call.message.answer(
        "Kanalni yuboring:\n\n"
        "• Ochiq kanal: <code>@kanal_username</code>\n"
        "• Yopiq kanal: <code>-1001234567890 https://t.me/+xxxx</code> (ID va taklif havolasi)\n\n"
        "⚠️ Bot avval kanalga <b>admin</b> qilib qo'shilgan bo'lishi kerak.\n/cancel — bekor qilish",
        parse_mode="HTML")
    await call.answer()


@admin.message(S.add_channel, F.text)
async def add_channel(m: Message, state: FSMContext):
    parts = m.text.split()
    ref = parts[0]
    try:
        if ref.startswith("@"):
            chat = await bot.get_chat(ref)
        elif ref.lstrip("-").isdigit():
            chat = await bot.get_chat(int(ref))
        else:
            return await m.answer("❌ Format noto'g'ri. @username yoki -100... ID yuboring.")
        me = await bot.get_chat_member(chat.id, bot.id)
        if me.status not in ("administrator", "creator"):
            return await m.answer("❌ Bot bu kanalda admin emas. Avval admin qiling.")
    except Exception as ex:
        return await m.answer(f"❌ Kanal topilmadi yoki bot unga kira olmaydi.\n<code>{e(ex)}</code>",
                              parse_mode="HTML")
    if len(parts) > 1:
        link = parts[1]
    elif chat.username:
        link = f"https://t.me/{chat.username}"
    else:
        return await m.answer("❌ Yopiq kanal uchun taklif havolasini ham yozing.")
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO channels(chat_id,title,link) VALUES($1,$2,$3) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=$2, link=$3",
            chat.id, chat.title or str(chat.id), link)
    await state.clear()
    await alog(m.from_user, f"Kanal qo'shildi: {chat.title}")
    await m.answer(f"✅ Qo'shildi: <b>{e(chat.title)}</b>", parse_mode="HTML",
                   reply_markup=menu(m.from_user.id))


# ---- 5. Broadcast ----
@admin.callback_query(F.data == "ad:bc")
async def ad_bc(call: CallbackQuery, state: FSMContext):
    await state.set_state(S.broadcast)
    await call.message.answer(
        "📢 Hammaga yuboriladigan xabarni yuboring (matn, rasm, video — istalgani).\n/cancel — bekor.")
    await call.answer()


@admin.message(S.broadcast)
async def bc_message(m: Message, state: FSMContext):
    await state.update_data(cid=m.chat.id, mid=m.message_id)
    async with pool.acquire() as c:
        n = await c.fetchval("SELECT count(*) FROM users WHERE active AND NOT banned")
    await m.answer(f"Bu xabar <b>{n}</b> ta foydalanuvchiga yuboriladi. Tasdiqlaysizmi?",
                   parse_mode="HTML",
                   reply_markup=kb([[("✅ Yuborish", "ad:bcgo"), ("❌ Bekor", "ad:home")]]))


async def do_broadcast(admin_id, cid, mid):
    async with pool.acquire() as c:
        ids = [r["id"] for r in await c.fetch("SELECT id FROM users WHERE active AND NOT banned")]
    ok = fail = 0
    for uid in ids:
        try:
            await bot.copy_message(uid, cid, mid)
            ok += 1
        except TelegramRetryAfter as ex:
            await asyncio.sleep(ex.retry_after)
            try:
                await bot.copy_message(uid, cid, mid)
                ok += 1
            except Exception:
                fail += 1
        except TelegramForbiddenError:
            fail += 1
            async with pool.acquire() as c:
                await c.execute("UPDATE users SET active=FALSE WHERE id=$1", uid)
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await bot.send_message(admin_id, f"✅ Yuborildi: {ok}\n❌ Yetib bormadi: {fail}")


@admin.callback_query(F.data == "ad:bcgo")
async def bc_go(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if "mid" not in data:
        return await call.answer("Xabar topilmadi.", show_alert=True)
    await alog(call.from_user, "Broadcast yubordi")
    t = asyncio.create_task(do_broadcast(call.from_user.id, data["cid"], data["mid"]))
    BG.add(t)
    t.add_done_callback(BG.discard)
    await show(call, "⏳ Yuborish boshlandi. Tugagach xabar beraman.", kb([BACK]))


# ---- 12/14/6. Sozlamalar, limit, maintenance, test rejimi ----
async def settings_view(call: CallbackQuery):
    rows = [[(f"{'✅' if CFG[k] == '1' else '❌'} {label}", f"ad:tg:{k}:set")]
            for k, label in TOGGLES.items()]
    rows.append([(f"🔢 Kunlik limit: {CFG['daily_limit']} (0 = cheksiz)", "ad:tx:daily_limit")])
    rows += [[(label, f"ad:tx:{k}")] for k, label in TEXTS.items()]
    rows.append(BACK)
    await show(call, "⚙️ <b>Sozlamalar</b>\nTugmani bosib yoqing/o'chiring. Limit — har bir user uchun kuniga "
                     "nechta yuklash (PRO va adminlar uchun cheksiz).", kb(rows))


@admin.callback_query(F.data == "ad:set")
async def ad_set(call: CallbackQuery):
    await settings_view(call)


@admin.callback_query(F.data.startswith("ad:tg:"))
async def ad_toggle(call: CallbackQuery):
    _, _, key, back = call.data.split(":")
    if key not in TOGGLES:
        return await call.answer()
    await set_cfg(key, "0" if CFG[key] == "1" else "1")
    await alog(call.from_user, f"{TOGGLES[key]}: {'yoqildi' if CFG[key] == '1' else 'o`chirildi'}")
    if back == "ch":
        await channels_view(call)
    elif back == "home":
        await show(call, PANEL_TITLE, menu(call.from_user.id))
    else:
        await settings_view(call)


@admin.callback_query(F.data == "ad:test")
async def ad_test(call: CallbackQuery):
    uid = call.from_user.id
    if uid in USER_MODE:
        USER_MODE.discard(uid)
        note = "🧪 Test rejimi o'chirildi — yana adminsiz."
    else:
        USER_MODE.add(uid)
        note = ("🧪 Test rejimi YOQILDI: endi bot sizga oddiy foydalanuvchidek ishlaydi "
                "(obuna, limit, maintenance, o'chirilgan funksiyalar). "
                "Panel tugmasi ishlayveradi. Tugatish uchun shu tugmani yana bosing.")
    await alog(call.from_user, "Test rejimi o'zgartirildi")
    await call.message.answer(note)
    await show(call, PANEL_TITLE, menu(uid))


@admin.callback_query(F.data.startswith("ad:tx:"))
async def ad_tx(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[2]
    await state.set_state(S.edit_text)
    await state.update_data(key=key)
    if key == "daily_limit":
        txt = f"Hozirgi limit: <b>{CFG[key]}</b>\n\nYangi sonni yuboring (0 = cheksiz). /cancel — bekor."
    else:
        hint = "\n\n<code>{bot}</code> — bot username'i o'rniga qo'yiladi." if key == "caption" else ""
        txt = f"Hozirgi matn:\n\n{e(CFG[key])}\n\nYangi matnni yuboring. /cancel — bekor.{hint}"
    await call.message.answer(txt, parse_mode="HTML")
    await call.answer()


@admin.message(S.edit_text, F.text)
async def tx_save(m: Message, state: FSMContext):
    data = await state.get_data()
    key = data["key"]
    if key == "daily_limit":
        if not m.text.strip().isdigit():
            return await m.answer("❌ Faqat son yuboring (masalan 20) yoki /cancel.")
        await set_cfg(key, m.text.strip())
    else:
        await set_cfg(key, m.html_text)
    await state.clear()
    await alog(m.from_user, f"O'zgartirdi: {key}")
    await m.answer("✅ Saqlandi.", reply_markup=menu(m.from_user.id))


# ---- 15. Admin activity log ----
@admin.callback_query(F.data == "ad:alog")
async def ad_alog(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM admin_log ORDER BY ts DESC LIMIT 20")
    text = "📝 <b>Admin Activity Log</b>\n\n"
    text += "\n".join(f"{r['ts']:%d.%m %H:%M} — <b>{e(r['admin_name'])}</b>: {e(r['action'])}"
                      for r in rows) or "Hali amal yo'q."
    await show(call, text, kb([BACK]))


# ---- adminlar ----
async def admins_view(call: CallbackQuery):
    text = "👮 <b>Adminlar</b>\n\n"
    text += "\n".join(f"• <code>{i}</code> (asosiy)" for i in sorted(OWNERS))
    extra = sorted(ADMIN_SET - OWNERS)
    if extra:
        text += "\n" + "\n".join(f"• <code>{i}</code>" for i in extra)
    rows = []
    if call.from_user.id in OWNERS:
        rows += [[(f"🗑 {i}", f"ad:admdel:{i}")] for i in extra]
        rows.append([("➕ Admin qo'shish", "ad:admadd")])
    rows.append(BACK)
    await show(call, text, kb(rows))


@admin.callback_query(F.data == "ad:adm")
async def ad_adm(call: CallbackQuery):
    await admins_view(call)


@admin.callback_query(F.data == "ad:admadd")
async def ad_admadd(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in OWNERS:
        return await call.answer("Faqat asosiy admin.", show_alert=True)
    await state.set_state(S.add_admin)
    await call.message.answer("Yangi adminning Telegram ID raqamini yuboring. /cancel — bekor.")
    await call.answer()


@admin.message(S.add_admin, F.text)
async def add_admin(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await m.answer("❌ Faqat raqamli ID yuboring.")
    uid = int(m.text.strip())
    async with pool.acquire() as c:
        await c.execute("INSERT INTO admins(id) VALUES($1) ON CONFLICT DO NOTHING", uid)
    ADMIN_SET.add(uid)
    await state.clear()
    await alog(m.from_user, f"Admin qo'shildi: {uid}")
    await m.answer(f"✅ Admin qo'shildi: <code>{uid}</code>\nU botga /start yozsa, «{ADMIN_BTN}» tugmasi chiqadi.",
                   parse_mode="HTML", reply_markup=menu(m.from_user.id))


@admin.callback_query(F.data.startswith("ad:admdel:"))
async def ad_admdel(call: CallbackQuery):
    if call.from_user.id not in OWNERS:
        return await call.answer("Faqat asosiy admin.", show_alert=True)
    uid = int(call.data.split(":")[2])
    async with pool.acquire() as c:
        await c.execute("DELETE FROM admins WHERE id=$1", uid)
    ADMIN_SET.discard(uid)
    await alog(call.from_user, f"Admin o'chirildi: {uid}")
    await admins_view(call)


# ====================== ISHGA TUSHIRISH ======================
async def health(_):
    return web.Response(text="ok")


async def main_run():
    global BOT_USERNAME
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL kiritilmagan!")
    if not OWNERS:
        logging.warning("ADMIN_IDS bo'sh — admin panel hech kimga ochilmaydi!")
    await db_init()
    BOT_USERNAME = (await bot.get_me()).username

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8000"))).start()

    dp.include_router(admin)
    dp.include_router(main)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main_run())
