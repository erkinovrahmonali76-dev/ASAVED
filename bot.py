import os
import re
import html
import uuid
import shutil
import asyncio
import logging
import tempfile
import subprocess
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
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
OWNERS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
COOKIES = os.getenv("COOKIES_FILE")
MAX_SIZE = 50 * 1024 * 1024

bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())
admin = Router()
main = Router()

BOT_USERNAME = os.getenv("BOT_USERNAME", "asaved_bot")
URL_RE = re.compile(r"https?://\S+")
SUPPORTED = ("instagram.com", "youtube.com", "youtu.be", "tiktok.com")
SEARCHES, LINKS = {}, {}
BG = set()

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
    "sub_enabled": "1",
    "feat_video": "1",
    "feat_music": "1",
    "feat_round": "1",
}
CFG = {}
TOGGLES = {
    "sub_enabled": "Majburiy obuna",
    "feat_video": "Video yuklash",
    "feat_music": "Musiqa qidirish",
    "feat_round": "Dumaloq video",
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


async def log_dl(uid, kind):
    try:
        async with pool.acquire() as c:
            await c.execute("INSERT INTO downloads(user_id, kind) VALUES($1,$2)", uid, kind)
    except Exception:
        logging.exception("log_dl")


def e(x) -> str:
    return html.escape(str(x or ""))


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows
    ])


def remember(store: dict, key, value):
    store[key] = value
    if len(store) > 500:
        store.pop(next(iter(store)))


# ====================== MAJBURIY OBUNA ======================
async def unsubscribed(user_id: int):
    async with pool.acquire() as c:
        chans = await c.fetch("SELECT chat_id, title, link FROM channels")
    missing = []
    for ch in chans:
        try:
            m = await bot.get_chat_member(ch["chat_id"], user_id)
            if m.status in ("left", "kicked"):
                missing.append(ch)
        except Exception:
            pass  # bot kanalda admin emas bo'lsa, bloklamaymiz
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
                "INSERT INTO users(id,name,username) VALUES($1,$2,$3) "
                "ON CONFLICT(id) DO UPDATE SET name=$2, username=$3, active=TRUE "
                "RETURNING banned", user.id, user.full_name, user.username)

        if is_admin(user.id):
            return await handler(event, data)

        if banned:
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Siz bloklangansiz.", show_alert=True)
            else:
                await event.answer("🚫 Siz bloklangansiz.")
            return

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
    o = {"quiet": True, "noplaylist": True, "no_warnings": True}
    if COOKIES and os.path.exists(COOKIES):
        local = "/tmp/cookies.txt"
        if not os.path.exists(local):
            shutil.copy(COOKIES, local)
        o["cookiefile"] = local
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


def _download_audio(url: str, folder: str):
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
    if info.get("entries"):
        info = info["entries"][0]
    return f"{folder}/{info['id']}.mp3", info


def download_audio(url: str, folder: str, fallback: str = None):
    try:
        return _download_audio(url, folder)
    except Exception:
        if not fallback:
            raise
        logging.exception("YouTube xato, SoundCloud'dan urinaman")
        return _download_audio(f"scsearch1:{fallback}", folder)


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
    await m.answer(CFG["start_text"], parse_mode="HTML")


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
    if CFG["feat_round"] != "1":
        return await m.answer(OFF)
    if m.video.file_size and m.video.file_size > 20 * 1024 * 1024:
        return await m.answer("⚠️ Video 20 MB dan oshmasligi kerak.")
    wait = await m.answer("⏳ Dumaloq video tayyorlanmoqda...")
    tmp = tempfile.mkdtemp()
    try:
        src, dst = f"{tmp}/in.mp4", f"{tmp}/round.mp4"
        await bot.download(m.video, destination=src)
        await asyncio.to_thread(make_round, src, dst)
        await m.answer_video_note(FSInputFile(dst), length=640)
        await log_dl(m.from_user.id, "round")
    except Exception:
        logging.exception("round error")
        await m.answer("❌ Dumaloq video qilib bo'lmadi.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        await wait.delete()


@main.message(F.text, ~F.text.startswith("/"))
async def text_handler(m: Message):
    text = m.text.strip()
    link = URL_RE.search(text)

    if link:
        if CFG["feat_video"] != "1":
            return await m.answer(OFF)
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
            await m.answer_video(
                FSInputFile(path),
                caption=CFG["caption"].replace("{bot}", BOT_USERNAME),
                reply_markup=kb([[("🎵 Qo'shiqni yuklab olish", f"a:{lid}")]]),
            )
            await log_dl(m.from_user.id, "video")
        except Exception:
            logging.exception("video error")
            await m.answer("❌ Yuklab bo'lmadi. Havola yopiq yoki xato bo'lishi mumkin.")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            await wait.delete()
        return

    if CFG["feat_music"] != "1":
        return await m.answer(OFF)
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
    lines = [f"🔍 <i>{e(text)}</i>\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"<b>{i}.</b> {e(r['title'])} <b>{fmt_dur(r['dur'])}</b>")
    nums = [InlineKeyboardButton(text=str(i), callback_data=f"s:{sid}:{i-1}")
            for i in range(1, len(results) + 1)]
    rows = [nums[i:i + 5] for i in range(0, len(nums), 5)]
    rows.append([InlineKeyboardButton(text="❌", callback_data="x")])
    await m.answer("\n".join(lines), parse_mode="HTML",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def send_mp3(call: CallbackQuery, url: str, fallback: str = None):
    wait = await call.message.answer("⏳ MP3 tayyorlanmoqda...")
    tmp = tempfile.mkdtemp()
    try:
        path, info = await asyncio.to_thread(download_audio, url, tmp, fallback)
        if os.path.getsize(path) > MAX_SIZE:
            return await call.message.answer("⚠️ Fayl 50 MB dan katta.")
        await call.message.answer_audio(
            FSInputFile(path),
            title=info.get("title"),
            performer=info.get("uploader"),
            duration=info.get("duration"),
            caption=f"🎵 @{BOT_USERNAME}",
        )
        await log_dl(call.from_user.id, "music")
    except Exception:
        logging.exception("audio error")
        await call.message.answer("❌ MP3 qilib bo'lmadi.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        await wait.delete()


@main.callback_query(F.data.startswith("s:"))
async def pick_song(call: CallbackQuery):
    _, sid, idx = call.data.split(":")
    results = SEARCHES.get(sid)
    if not results:
        return await call.answer("Qidiruv eskirgan, qaytadan yozing.", show_alert=True)
    await call.answer("Yuklanmoqda...")
    song = results[int(idx)]
    await send_mp3(call, f"https://www.youtube.com/watch?v={song['id']}", song["title"])


@main.callback_query(F.data.startswith("a:"))
async def audio_from_link(call: CallbackQuery):
    url = LINKS.get(call.data[2:])
    if not url:
        return await call.answer("Havola eskirgan.", show_alert=True)
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


MENU = kb([
    [("📊 Statistika", "ad:stats"), ("📢 Majburiy kanallar", "ad:ch")],
    [("👥 Foydalanuvchilar", "ad:users"), ("📣 Xabar yuborish", "ad:bc")],
    [("⚙️ Sozlamalar", "ad:set"), ("👮 Adminlar", "ad:adm")],
])
BACK = [("⬅️ Orqaga", "ad:home")]


async def show(call: CallbackQuery, text: str, markup):
    try:
        await call.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await call.answer()


@admin.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Admin panel</b>", reply_markup=MENU, parse_mode="HTML")


@admin.message(Command("cancel"))
async def cancel_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=MENU)


@admin.callback_query(F.data == "ad:home")
async def ad_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, "🛠 <b>Admin panel</b>", MENU)


# ---- statistika ----
@admin.callback_query(F.data == "ad:stats")
async def ad_stats(call: CallbackQuery):
    async with pool.acquire() as c:
        u = await c.fetchrow("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE active) AS act,
                   count(*) FILTER (WHERE banned) AS ban,
                   count(*) FILTER (WHERE joined > now() - interval '1 day') AS d1,
                   count(*) FILTER (WHERE joined > now() - interval '7 days') AS d7
            FROM users""")
        d = await c.fetch("""
            SELECT kind, count(*) AS total,
                   count(*) FILTER (WHERE ts > now() - interval '1 day') AS d1
            FROM downloads GROUP BY kind""")
        nch = await c.fetchval("SELECT count(*) FROM channels")
    names = {"video": "🎬 Video", "music": "🎵 Musiqa", "round": "⭕ Dumaloq"}
    dl = "\n".join(f"{names.get(r['kind'], r['kind'])}: {r['total']} (bugun {r['d1']})" for r in d) or "—"
    text = (
        "📊 <b>Statistika</b>\n\n"
        f"👥 Jami: <b>{u['total']}</b>\n"
        f"✅ Faol: {u['act']}   🚫 Bloklangan: {u['ban']}\n"
        f"🆕 Bugun: {u['d1']}   Hafta: {u['d7']}\n"
        f"📢 Majburiy kanallar: {nch}\n\n"
        f"<b>Yuklashlar:</b>\n{dl}"
    )
    await show(call, text, kb([BACK]))


# ---- majburiy kanallar ----
async def channels_view(call: CallbackQuery):
    async with pool.acquire() as c:
        chans = await c.fetch("SELECT chat_id, title FROM channels ORDER BY title")
    on = CFG["sub_enabled"] == "1"
    state_txt = "✅ yoqilgan" if on else "❌ o'chirilgan"
    text = f"📢 <b>Majburiy kanallar</b>\nObuna tekshiruvi: {state_txt}\n\n"
    text += "\n".join(f"• {e(ch['title'])}" for ch in chans) or "Hali kanal qo'shilmagan."
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
        await c.execute("DELETE FROM channels WHERE chat_id=$1", cid)
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
    await m.answer(f"✅ Qo'shildi: <b>{e(chat.title)}</b>", parse_mode="HTML", reply_markup=MENU)


# ---- foydalanuvchilar ----
async def user_card(uid: int):
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT * FROM users WHERE id=$1", uid)
        n = await c.fetchval("SELECT count(*) FROM downloads WHERE user_id=$1", uid)
    if not r:
        return None
    text = (
        f"👤 <b>{e(r['name'])}</b>\n"
        f"ID: <code>{r['id']}</code>\n"
        f"Username: {'@' + e(r['username']) if r['username'] else '—'}\n"
        f"Qo'shilgan: {r['joined']:%Y-%m-%d %H:%M}\n"
        f"Yuklashlar: {n}\n"
        f"Holat: {'🚫 bloklangan' if r['banned'] else '✅ faol'}"
    )
    btn = ("✅ Blokdan chiqarish", f"ad:unban:{uid}") if r["banned"] else ("🚫 Bloklash", f"ad:ban:{uid}")
    return text, kb([[btn], [("⬅️ Orqaga", "ad:users")]])


@admin.callback_query(F.data == "ad:users")
async def ad_users(call: CallbackQuery, state: FSMContext):
    await state.clear()
    async with pool.acquire() as c:
        total = await c.fetchval("SELECT count(*) FROM users")
        last = await c.fetch("SELECT id, name FROM users ORDER BY joined DESC LIMIT 10")
    text = f"👥 <b>Foydalanuvchilar: {total}</b>\n\nOxirgi 10 ta:\n"
    text += "\n".join(f"• {e(r['name'])} — <code>{r['id']}</code>" for r in last)
    await show(call, text, kb([
        [("🔎 Qidirish (ID/@username)", "ad:find")],
        [("📄 Ro'yxatni yuklab olish", "ad:export")],
        BACK,
    ]))


@admin.callback_query(F.data == "ad:export")
async def ad_export(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM users ORDER BY joined")
    lines = [f"{r['id']} | {r['name']} | @{r['username'] or '-'} | {r['joined']:%Y-%m-%d} | "
             f"{'banned' if r['banned'] else 'ok'}" for r in rows]
    data = "\n".join(lines).encode()
    await call.message.answer_document(BufferedInputFile(data, "users.txt"),
                                       caption=f"Jami: {len(rows)}")
    await call.answer()


@admin.callback_query(F.data == "ad:find")
async def ad_find(call: CallbackQuery, state: FSMContext):
    await state.set_state(S.find_user)
    await call.message.answer("Foydalanuvchi ID yoki @username yuboring. /cancel — bekor.")
    await call.answer()


@admin.message(S.find_user, F.text)
async def find_user(m: Message, state: FSMContext):
    q = m.text.strip().lstrip("@")
    async with pool.acquire() as c:
        if q.isdigit():
            uid = await c.fetchval("SELECT id FROM users WHERE id=$1", int(q))
        else:
            uid = await c.fetchval("SELECT id FROM users WHERE lower(username)=lower($1)", q)
    if not uid:
        return await m.answer("😕 Topilmadi. Boshqa ID/username yuboring yoki /cancel.")
    await state.clear()
    text, markup = await user_card(uid)
    await m.answer(text, parse_mode="HTML", reply_markup=markup)


@admin.callback_query(F.data.regexp(r"^ad:(ban|unban):\d+$"))
async def ad_ban(call: CallbackQuery):
    _, act, uid = call.data.split(":")
    uid = int(uid)
    if is_admin(uid):
        return await call.answer("Adminni bloklab bo'lmaydi.", show_alert=True)
    async with pool.acquire() as c:
        await c.execute("UPDATE users SET banned=$2 WHERE id=$1", uid, act == "ban")
    text, markup = await user_card(uid)
    await show(call, text, markup)


# ---- xabar yuborish ----
@admin.callback_query(F.data == "ad:bc")
async def ad_bc(call: CallbackQuery, state: FSMContext):
    await state.set_state(S.broadcast)
    await call.message.answer(
        "📣 Hammaga yuboriladigan xabarni yuboring (matn, rasm, video — istalgani).\n/cancel — bekor.")
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
    t = asyncio.create_task(do_broadcast(call.from_user.id, data["cid"], data["mid"]))
    BG.add(t)
    t.add_done_callback(BG.discard)
    await show(call, "⏳ Yuborish boshlandi. Tugagach xabar beraman.", kb([BACK]))


# ---- sozlamalar ----
async def settings_view(call: CallbackQuery):
    rows = [[(f"{'✅' if CFG[k] == '1' else '❌'} {label}", f"ad:tg:{k}:set")]
            for k, label in TOGGLES.items()]
    rows.append([("✏️ Start matni", "ad:tx:start_text")])
    rows.append([("✏️ Video izohi (caption)", "ad:tx:caption")])
    rows.append(BACK)
    await show(call, "⚙️ <b>Sozlamalar</b>\nTugmani bosib yoqing/o'chiring.", kb(rows))


@admin.callback_query(F.data == "ad:set")
async def ad_set(call: CallbackQuery):
    await settings_view(call)


@admin.callback_query(F.data.startswith("ad:tg:"))
async def ad_toggle(call: CallbackQuery):
    _, _, key, back = call.data.split(":")
    if key not in TOGGLES:
        return await call.answer()
    await set_cfg(key, "0" if CFG[key] == "1" else "1")
    if back == "ch":
        await channels_view(call)
    else:
        await settings_view(call)


@admin.callback_query(F.data.startswith("ad:tx:"))
async def ad_tx(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[2]
    await state.set_state(S.edit_text)
    await state.update_data(key=key)
    hint = "\n\n<code>{bot}</code> — bot username'i o'rniga qo'yiladi." if key == "caption" else ""
    await call.message.answer(
        f"Hozirgi matn:\n\n{e(CFG[key])}\n\nYangi matnni yuboring. /cancel — bekor.{hint}",
        parse_mode="HTML")
    await call.answer()


@admin.message(S.edit_text, F.text)
async def tx_save(m: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await set_cfg(data["key"], m.html_text)
    await m.answer("✅ Saqlandi.", reply_markup=MENU)


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
    await m.answer(f"✅ Admin qo'shildi: <code>{uid}</code>", parse_mode="HTML", reply_markup=MENU)


@admin.callback_query(F.data.startswith("ad:admdel:"))
async def ad_admdel(call: CallbackQuery):
    if call.from_user.id not in OWNERS:
        return await call.answer("Faqat asosiy admin.", show_alert=True)
    uid = int(call.data.split(":")[2])
    async with pool.acquire() as c:
        await c.execute("DELETE FROM admins WHERE id=$1", uid)
    ADMIN_SET.discard(uid)
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
