import os
import re
import io
import sys
import json
import gzip
import html
import time
import uuid
import shutil
import asyncio
import logging
import tempfile
import threading
import subprocess
import importlib.metadata as md
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# yt-dlp tez-tez eskiradi (YouTube o'zgaradi). Ishga tushishda eng yangisini o'rnatib olamiz.
# O'chirish uchun: AUTO_UPDATE_YTDLP=0
if os.getenv("AUTO_UPDATE_YTDLP", "1") == "1":
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "--pre", "--quiet",
             "--disable-pip-version-check", "yt-dlp[default]"],
            timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if subprocess.run([sys.executable, "-c", "import yt_dlp"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            raise RuntimeError("yt-dlp yo'q")
    except Exception:
        pass

import aiohttp
import yt_dlp
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.filters import CommandStart, Command, CommandObject, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter,
)
from aiogram.types import (
    Message, CallbackQuery, FSInputFile, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
    LabeledPrice, PreCheckoutQuery,
)

try:
    from shazamio import Shazam
except ImportError:  # requirements.txt ga shazamio qo'shilmagan bo'lsa
    Shazam = None

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
OWNERS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
COOKIES = os.getenv("COOKIES_FILE")
PROXY = os.getenv("PROXY_URL")
PRO_QUALITY = os.getenv("PRO_QUALITY", "192")  # PRO uchun kbps (192 yoki 320)
MAX_SIZE = 50 * 1024 * 1024
AUDIO_QUALITY = os.getenv("AUDIO_QUALITY", "128")  # kbps
SEARCH_N = int(os.getenv("SEARCH_N", "20"))  # qidiruvda nechta natija olinadi (10 tadan sahifalanadi)
DL_BUDGET = int(os.getenv("DL_BUDGET", "75"))  # bitta qo'shiq uchun umumiy vaqt chegarasi (soniya)
MAX_DL = int(os.getenv("MAX_DL", "4"))  # bir vaqtda nechta yuklash/konvertatsiya
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "5"))  # Toshkent = UTC+5
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "9"))  # kunlik hisobot (mahalliy soat)
AUDD_TOKEN = os.getenv("AUDD_TOKEN")  # ixtiyoriy: Shazam topolmasa AudD sinaladi
PAY_TOKEN = os.getenv("PAY_PROVIDER_TOKEN")  # BotFather → Payments (Click/Payme)
PAY_CURRENCY = os.getenv("PAY_CURRENCY", "UZS")
LOCAL = timezone(timedelta(hours=TZ_OFFSET))
COOKIE_LOCAL = "/tmp/yt_cookies.txt"
TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

PRO_PLANS = {}  # PRO_PLANS="30:15000,90:39000"  (kun:narx, so'mda)
for _p in os.getenv("PRO_PLANS", "").split(","):
    if ":" in _p:
        _d, _pr = _p.split(":", 1)
        if _d.strip().isdigit() and _pr.strip().isdigit():
            PRO_PLANS[int(_d)] = int(_pr)

bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())
admin = Router()
main = Router()

# Yuklash/qidirish uchun alohida thread pool: foydalanuvchilar bir-birini kutib qolmasin
EXECUTOR = ThreadPoolExecutor(max_workers=int(os.getenv("WORKERS", "16")))
DL_SEM = asyncio.Semaphore(MAX_DL)


async def run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(EXECUTOR, fn, *args)


BOT_USERNAME = os.getenv("BOT_USERNAME", "asaved_bot")
URL_RE = re.compile(r"https?://\S+")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
YT_ID_RE = re.compile(r"(?:v=|youtu\.be/)([\w-]{11})")
YT_ID_OK = re.compile(r"[\w-]{11}")
SUPPORTED = ("instagram.com", "youtube.com", "youtu.be", "tiktok.com")
SEARCHES, LINKS, INLINE_META = {}, {}, {}
SEARCH_CACHE = {}
ALERT_LAST = {}
BUSY = set()  # hozir yuklayotgan userlar (bir vaqtda bitta)
BG = set()
ADMIN_BTN = "⚙️ Boshqarish"

# ====================== TILLAR ======================
STR = {
    "uz": {
        "start": "👋 Salom! Men <b>ASAVED BOT</b>.\n\n"
                 "📥 Instagram / YouTube / TikTok <b>havolasini</b> yuboring — videoni yuklab beraman.\n"
                 "🎵 <b>Qo'shiq yoki ijrochi nomini</b> yozing — natijalarni chiqaraman.\n"
                 "🎤 <b>Ovozli xabar</b> yoki audio yuboring — qo'shiqni topib beraman.\n"
                 "⭕ <b>Video yuboring</b> — dumaloq video qilib beraman.\n\n"
                 "💡 Pastdagi tugmalardan foydalaning.",
        "off": "⛔ Bu funksiya vaqtincha o'chirilgan.",
        "banned": "🚫 Siz bloklangansiz.",
        "maint": "🛠 Botda texnik ishlar ketmoqda. Birozdan so'ng qaytadan urinib ko'ring.",
        "pro_text": "💎 Cheksiz foydalanish uchun PRO oling: /buy",
        "sub_gate": "❗ Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling, so'ng «Tekshirish» ni bosing:",
        "check_btn": "✅ Tekshirish",
        "sub_missing": "❌ Hali hamma kanalga obuna bo'lmadingiz.",
        "thanks": "✅ Rahmat!",
        "limit": "⚠️ Bugungi limit ({n} ta) tugadi. Ertaga yangilanadi.\n\n{pro}",
        "round_big": "⚠️ Video 20 MB dan oshmasligi kerak.",
        "round_wait": "⏳ Dumaloq video tayyorlanmoqda...",
        "round_fail": "❌ Dumaloq video qilib bo'lmadi.",
        "only_sup": "⚠️ Faqat Instagram, YouTube va TikTok havolalari.",
        "wait_dl": "⏳ Yuklanmoqda...",
        "video_big": "⚠️ Video 50 MB dan katta, yuborib bo'lmaydi.",
        "video_fail": "❌ Yuklab bo'lmadi. Havola yopiq yoki xato bo'lishi mumkin.",
        "dl_audio_btn": "🎵 Qo'shiqni yuklab olish",
        "searching": "🔍 Qidirilmoqda...",
        "nothing": "😕 Hech narsa topilmadi.",
        "wait_mp3": "⏳ MP3 tayyorlanmoqda...",
        "file_big": "⚠️ Fayl 50 MB dan katta.",
        "mp3_fail": "❌ MP3 qilib bo'lmadi. Keyinroq urinib ko'ring.",
        "busy": "⏳ Oldingi yuklash tugaguncha kuting.",
        "stale_search": "Qidiruv eskirgan, qaytadan yozing.",
        "stale_link": "Havola eskirgan.",
        "loading": "Yuklanmoqda...",
        "listening": "🎧 Tinglanmoqda...",
        "file_big20": "⚠️ Fayl 20 MB dan oshmasligi kerak.",
        "not_recognized": "😕 Qo'shiq aniqlanmadi.\n\n"
                          "• Musiqa aniq va baland eshitiladigan 10–20 soniyalik ovoz yuboring\n"
                          "• Shovqin kam bo'lsin\n"
                          "• Xirgoyi qilib yoki o'zingiz kuylagan qo'shiq aniqlanmaydi\n\n"
                          "✍️ Yoki qo'shiq nomini yozing — qidirib beraman.",
        "recog_fail": "❌ Qo'shiqni aniqlab bo'lmadi. Keyinroq urinib ko'ring yoki nomini yozing.",
        "recog_nores": "{head}\n\n😕 Yuklab olish uchun natija topilmadi. Nomini matn qilib yozib ko'ring.",
        "fav_btn": "⭐ Saqlash",
        "fav_saved": "⭐ Saqlandi!",
        "fav_removed": "🗑 O'chirildi",
        "fav_empty": "Sevimli qo'shiqlar yo'q.\nQo'shiq ostidagi «⭐ Saqlash» tugmasini bosing.",
        "fav_title": "⭐ <b>Sevimli qo'shiqlar</b>\nYuklash uchun nomini bosing:",
        "top_title": "🔥 <b>Eng ko'p yuklanganlar</b>\nYuklash uchun raqamni bosing:",
        "top_empty": "Hali ma'lumot yo'q.",
        "ref_text": "🎁 <b>Do'stlarni taklif qiling</b>\n\nSizning havolangiz:\n{link}\n\n"
                    "Har bir yangi do'st uchun <b>{days} kun PRO</b> beriladi.\nTaklif qilinganlar: <b>{n}</b>",
        "ref_new": "🎉 Sizning havolangiz orqali yangi foydalanuvchi qo'shildi! +{days} kun PRO.",
        "lang_pick": "🌐 Tilni tanlang:",
        "lang_set": "✅ Til o'zgartirildi.",
        "promo_usage": "🎟 Promo-kod ishlatish: /promo KOD",
        "promo_ok": "✅ Promo-kod qabul qilindi! +{days} kun PRO.",
        "promo_bad": "❌ Promo-kod noto'g'ri yoki muddati tugagan.",
        "promo_used": "⚠️ Siz bu kodni allaqachon ishlatgansiz.",
        "pro_given": "💎 Sizga PRO berildi (+{days} kun). Cheksiz foydalaning!",
        "buy_none": "💎 PRO\n\n{pro}",
        "buy_pick": "💎 <b>PRO tarifini tanlang:</b>",
        "buy_title": "PRO — {days} kun",
        "buy_desc": "Cheksiz yuklash {days} kun davomida",
        "price_fmt": "{days} kun — {price} so'm",
        "paid": "✅ To'lov qabul qilindi! PRO +{days} kun.",
    },
    "ru": {
        "start": "👋 Привет! Я <b>ASAVED BOT</b>.\n\n"
                 "📥 Пришлите <b>ссылку</b> Instagram / YouTube / TikTok — скачаю видео.\n"
                 "🎵 Напишите <b>название песни или исполнителя</b> — покажу результаты.\n"
                 "🎤 Пришлите <b>голосовое</b> или аудио — найду песню.\n"
                 "⭕ Пришлите <b>видео</b> — сделаю круглое.\n\n"
                 "💡 Пользуйтесь кнопками внизу.",
        "off": "⛔ Эта функция временно отключена.",
        "banned": "🚫 Вы заблокированы.",
        "maint": "🛠 Ведутся технические работы. Попробуйте чуть позже.",
        "pro_text": "💎 Получите PRO для безлимитного использования: /buy",
        "sub_gate": "❗ Чтобы пользоваться ботом, подпишитесь на каналы ниже и нажмите «Проверить»:",
        "check_btn": "✅ Проверить",
        "sub_missing": "❌ Вы ещё не подписались на все каналы.",
        "thanks": "✅ Спасибо!",
        "limit": "⚠️ Дневной лимит ({n}) исчерпан. Обновится завтра.\n\n{pro}",
        "round_big": "⚠️ Видео не должно быть больше 20 МБ.",
        "round_wait": "⏳ Готовлю круглое видео...",
        "round_fail": "❌ Не удалось сделать круглое видео.",
        "only_sup": "⚠️ Только ссылки Instagram, YouTube и TikTok.",
        "wait_dl": "⏳ Загрузка...",
        "video_big": "⚠️ Видео больше 50 МБ, отправить нельзя.",
        "video_fail": "❌ Не удалось скачать. Ссылка закрыта или неверна.",
        "dl_audio_btn": "🎵 Скачать песню",
        "searching": "🔍 Ищу...",
        "nothing": "😕 Ничего не найдено.",
        "wait_mp3": "⏳ Готовлю MP3...",
        "file_big": "⚠️ Файл больше 50 МБ.",
        "mp3_fail": "❌ Не удалось сделать MP3. Попробуйте позже.",
        "busy": "⏳ Дождитесь окончания предыдущей загрузки.",
        "stale_search": "Поиск устарел, напишите заново.",
        "stale_link": "Ссылка устарела.",
        "loading": "Загрузка...",
        "listening": "🎧 Слушаю...",
        "file_big20": "⚠️ Файл не должен быть больше 20 МБ.",
        "not_recognized": "😕 Песня не распознана.\n\n"
                          "• Пришлите 10–20 секунд, где музыка слышна чётко и громко\n"
                          "• Поменьше шума\n"
                          "• Напетую или спетую вами песню распознать нельзя\n\n"
                          "✍️ Или напишите название — найду.",
        "recog_fail": "❌ Не удалось распознать песню. Попробуйте позже или напишите название.",
        "recog_nores": "{head}\n\n😕 Нет результатов для скачивания. Напишите название текстом.",
        "fav_btn": "⭐ Сохранить",
        "fav_saved": "⭐ Сохранено!",
        "fav_removed": "🗑 Удалено",
        "fav_empty": "Избранных песен нет.\nНажмите «⭐ Сохранить» под песней.",
        "fav_title": "⭐ <b>Избранное</b>\nНажмите на название, чтобы скачать:",
        "top_title": "🔥 <b>Самые скачиваемые</b>\nНажмите номер, чтобы скачать:",
        "top_empty": "Пока нет данных.",
        "ref_text": "🎁 <b>Пригласите друзей</b>\n\nВаша ссылка:\n{link}\n\n"
                    "За каждого нового друга — <b>{days} дн. PRO</b>.\nПриглашено: <b>{n}</b>",
        "ref_new": "🎉 По вашей ссылке пришёл новый пользователь! +{days} дн. PRO.",
        "lang_pick": "🌐 Выберите язык:",
        "lang_set": "✅ Язык изменён.",
        "promo_usage": "🎟 Использовать промокод: /promo КОД",
        "promo_ok": "✅ Промокод принят! +{days} дн. PRO.",
        "promo_bad": "❌ Промокод неверный или истёк.",
        "promo_used": "⚠️ Вы уже использовали этот код.",
        "pro_given": "💎 Вам выдан PRO (+{days} дн.). Пользуйтесь без ограничений!",
        "buy_none": "💎 PRO\n\n{pro}",
        "buy_pick": "💎 <b>Выберите тариф PRO:</b>",
        "buy_title": "PRO — {days} дн.",
        "buy_desc": "Безлимитные загрузки на {days} дн.",
        "price_fmt": "{days} дн. — {price} сум",
        "paid": "✅ Оплата принята! PRO +{days} дн.",
    },
    "en": {
        "start": "👋 Hi! I'm <b>ASAVED BOT</b>.\n\n"
                 "📥 Send an Instagram / YouTube / TikTok <b>link</b> — I'll download the video.\n"
                 "🎵 Type a <b>song or artist name</b> — I'll show results.\n"
                 "🎤 Send a <b>voice message</b> or audio — I'll identify the song.\n"
                 "⭕ Send a <b>video</b> — I'll make it a round video.\n\n"
                 "💡 Use the buttons below.",
        "off": "⛔ This feature is temporarily disabled.",
        "banned": "🚫 You are blocked.",
        "maint": "🛠 Maintenance in progress. Please try again later.",
        "pro_text": "💎 Get PRO for unlimited use: /buy",
        "sub_gate": "❗ To use the bot, subscribe to the channels below and tap “Check”:",
        "check_btn": "✅ Check",
        "sub_missing": "❌ You haven't subscribed to all channels yet.",
        "thanks": "✅ Thanks!",
        "limit": "⚠️ Daily limit ({n}) reached. It resets tomorrow.\n\n{pro}",
        "round_big": "⚠️ Video must not exceed 20 MB.",
        "round_wait": "⏳ Making your round video...",
        "round_fail": "❌ Couldn't make the round video.",
        "only_sup": "⚠️ Only Instagram, YouTube and TikTok links.",
        "wait_dl": "⏳ Downloading...",
        "video_big": "⚠️ Video is larger than 50 MB, can't send it.",
        "video_fail": "❌ Couldn't download. The link may be private or invalid.",
        "dl_audio_btn": "🎵 Download song",
        "searching": "🔍 Searching...",
        "nothing": "😕 Nothing found.",
        "wait_mp3": "⏳ Preparing MP3...",
        "file_big": "⚠️ File is larger than 50 MB.",
        "mp3_fail": "❌ Couldn't make the MP3. Please try again later.",
        "busy": "⏳ Please wait for your previous download to finish.",
        "stale_search": "Search expired, please type again.",
        "stale_link": "Link expired.",
        "loading": "Loading...",
        "listening": "🎧 Listening...",
        "file_big20": "⚠️ File must not exceed 20 MB.",
        "not_recognized": "😕 Couldn't identify the song.\n\n"
                          "• Send 10–20 seconds where the music is clear and loud\n"
                          "• Keep background noise low\n"
                          "• Humming or singing yourself can't be identified\n\n"
                          "✍️ Or type the song name — I'll search for it.",
        "recog_fail": "❌ Couldn't identify the song. Try again later or type its name.",
        "recog_nores": "{head}\n\n😕 No downloadable results. Try typing the name.",
        "fav_btn": "⭐ Save",
        "fav_saved": "⭐ Saved!",
        "fav_removed": "🗑 Removed",
        "fav_empty": "No favorite songs yet.\nTap “⭐ Save” under a song.",
        "fav_title": "⭐ <b>Favorites</b>\nTap a title to download:",
        "top_title": "🔥 <b>Most downloaded</b>\nTap a number to download:",
        "top_empty": "No data yet.",
        "ref_text": "🎁 <b>Invite friends</b>\n\nYour link:\n{link}\n\n"
                    "You get <b>{days} days of PRO</b> for each new friend.\nInvited: <b>{n}</b>",
        "ref_new": "🎉 A new user joined via your link! +{days} days PRO.",
        "lang_pick": "🌐 Choose your language:",
        "lang_set": "✅ Language changed.",
        "promo_usage": "🎟 Use a promo code: /promo CODE",
        "promo_ok": "✅ Promo code accepted! +{days} days PRO.",
        "promo_bad": "❌ Invalid or expired promo code.",
        "promo_used": "⚠️ You've already used this code.",
        "pro_given": "💎 You've been given PRO (+{days} days). Enjoy unlimited use!",
        "buy_none": "💎 PRO\n\n{pro}",
        "buy_pick": "💎 <b>Choose a PRO plan:</b>",
        "buy_title": "PRO — {days} days",
        "buy_desc": "Unlimited downloads for {days} days",
        "price_fmt": "{days} days — {price} UZS",
        "paid": "✅ Payment received! PRO +{days} days.",
    },
}

BTN = {
    "fav": {"uz": "⭐ Sevimlilar", "ru": "⭐ Избранное", "en": "⭐ Favorites"},
    "top": {"uz": "🔥 Top 10", "ru": "🔥 Топ 10", "en": "🔥 Top 10"},
    "ref": {"uz": "🎁 Do'st taklif qilish", "ru": "🎁 Пригласить друга", "en": "🎁 Invite friends"},
    "pro": {"uz": "💎 PRO", "ru": "💎 PRO", "en": "💎 PRO"},
    "lang": {"uz": "🌐 Til", "ru": "🌐 Язык", "en": "🌐 Language"},
}
BTN_REV = {t: k for k, d in BTN.items() for t in d.values()}


STR["uz"].update({
    "buy_method": "💳 To'lov usulini tanlang:",
    "m_auto": "💳 Click / Payme (avtomatik)",
    "m_card": "🏦 Kartaga o'tkazish (chek bilan)",
    "pay_card": "🏦 <b>Kartaga to'lov</b>\n\nTarif: <b>{days} kun PRO</b>\nSumma: <b>{price} so'm</b>\n\n{cards}\n\n"
                "1️⃣ Yuqoridagi kartaga aynan <b>{price} so'm</b> o'tkazing.\n"
                "2️⃣ To'lov <b>chekini</b> (skrinshot yoki rasm) shu yerga yuboring.\n"
                "3️⃣ Admin tasdiqlagach, PRO avtomatik yoqiladi.\n\nBekor qilish: /cancel",
    "chek_sent": "✅ Chek qabul qilindi. Admin tekshirib, tez orada PRO yoqadi.",
    "chek_need": "📸 Iltimos, to'lov chekini rasm (yoki fayl) qilib yuboring.\nBekor qilish: /cancel",
    "chek_limit": "⚠️ Sizda tekshirilmagan cheklar ko'p. Admin javobini kuting.",
    "chek_ok": "✅ To'lov tasdiqlandi! PRO +{days} kun yoqildi.",
    "chek_no": "❌ To'lov cheki rad etildi. Xato bo'lsa, qaytadan /buy qiling yoki admin bilan bog'laning.",
    "cancelled": "Bekor qilindi.",
})
STR["ru"].update({
    "buy_method": "💳 Выберите способ оплаты:",
    "m_auto": "💳 Click / Payme (автоматически)",
    "m_card": "🏦 Перевод на карту (с чеком)",
    "pay_card": "🏦 <b>Оплата на карту</b>\n\nТариф: <b>PRO {days} дн.</b>\nСумма: <b>{price} сум</b>\n\n{cards}\n\n"
                "1️⃣ Переведите ровно <b>{price} сум</b> на карту выше.\n"
                "2️⃣ Отправьте сюда <b>чек</b> (скриншот или фото).\n"
                "3️⃣ После подтверждения админом PRO включится автоматически.\n\nОтмена: /cancel",
    "chek_sent": "✅ Чек принят. Админ проверит и скоро включит PRO.",
    "chek_need": "📸 Пожалуйста, отправьте чек картинкой (или файлом).\nОтмена: /cancel",
    "chek_limit": "⚠️ У вас много непроверенных чеков. Дождитесь ответа админа.",
    "chek_ok": "✅ Оплата подтверждена! PRO +{days} дн. включён.",
    "chek_no": "❌ Чек отклонён. Если это ошибка, повторите /buy или свяжитесь с админом.",
    "cancelled": "Отменено.",
})
STR["en"].update({
    "buy_method": "💳 Choose a payment method:",
    "m_auto": "💳 Click / Payme (automatic)",
    "m_card": "🏦 Card transfer (with receipt)",
    "pay_card": "🏦 <b>Pay by card</b>\n\nPlan: <b>PRO {days} days</b>\nAmount: <b>{price} UZS</b>\n\n{cards}\n\n"
                "1️⃣ Transfer exactly <b>{price} UZS</b> to the card above.\n"
                "2️⃣ Send the <b>receipt</b> (screenshot or photo) here.\n"
                "3️⃣ PRO is activated automatically once the admin confirms.\n\nCancel: /cancel",
    "chek_sent": "✅ Receipt received. The admin will check it and activate PRO soon.",
    "chek_need": "📸 Please send the receipt as a photo (or file).\nCancel: /cancel",
    "chek_limit": "⚠️ You have many unchecked receipts. Please wait for the admin.",
    "chek_ok": "✅ Payment confirmed! PRO +{days} days activated.",
    "chek_no": "❌ Receipt rejected. If this is a mistake, try /buy again or contact the admin.",
    "cancelled": "Cancelled.",
})

ERR_KEYS = {"not_recognized", "recog_fail", "mp3_fail", "video_fail", "nothing", "round_fail", "recog_nores"}


def tr(lang: str, key: str, **kw) -> str:
    s = STR.get(lang, STR["uz"]).get(key) or STR["uz"][key]
    s = s.format(**kw) if kw else s
    if key in ERR_KEYS:  # xato xabarlarida ham bot nomi ko'rinsin
        s += f"\n\n🤖 @{BOT_USERNAME}"
    return s


async def is_pro(uid: int) -> bool:
    try:
        async with pool.acquire() as c:
            return bool(await c.fetchval("SELECT pro_until > now() FROM users WHERE id=$1", uid))
    except Exception:
        return False


def user_kb(uid: int, lang: str):
    rows = [
        [KeyboardButton(text=BTN["fav"][lang]), KeyboardButton(text=BTN["top"][lang])],
        [KeyboardButton(text=BTN["ref"][lang]), KeyboardButton(text=BTN["pro"][lang])],
        [KeyboardButton(text=BTN["lang"][lang])],
    ]
    if is_admin(uid):
        rows.append([KeyboardButton(text=ADMIN_BTN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ====================== DATABASE / SOZLAMALAR ======================
pool = None
ADMIN_SET = set()
DEFAULTS = {
    "start_text": STR["uz"]["start"],
    "caption": "📩 @{bot} orqali yuklab olindi",
    "pro_text": "💎 Cheksiz foydalanish uchun PRO oling — admin bilan bog'laning.",
    "maint_text": "🛠 Botda texnik ishlar ketmoqda. Birozdan so'ng qaytadan urinib ko'ring.",
    "daily_limit": "20",
    "ref_days": "3",
    "sub_enabled": "1",
    "maintenance": "0",
    "feat_video": "1",
    "feat_music": "1",
    "feat_round": "1",
    "feat_voice": "1",
    "last_report": "",
}
CFG = {}
TOGGLES = {
    "sub_enabled": "Majburiy obuna",
    "maintenance": "Texnik ishlar (maintenance)",
    "feat_video": "Video yuklash",
    "feat_music": "Musiqa qidirish",
    "feat_voice": "Ovoz orqali topish",
    "feat_round": "Dumaloq video",
}
NUMS = {
    "daily_limit": "🔢 Kunlik limit",
    "ref_days": "🎁 Referal bonusi (kun)",
}
TEXTS = {
    "start_text": "✏️ Start matni (o'zbekcha)",
    "caption": "✏️ Video izohi (caption)",
    "pro_text": "✏️ PRO matni (o'zbekcha)",
    "maint_text": "✏️ Maintenance matni (o'zbekcha)",
}


def clean_dsn(u: str) -> str:
    p = urlsplit(u)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "channel_binding"]
    return urlunsplit(p._replace(query=urlencode(q)))


async def db_init():
    global pool
    pool = await asyncpg.create_pool(clean_dsn(DATABASE_URL), min_size=1, max_size=8)
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
        ALTER TABLE users ADD COLUMN IF NOT EXISTS pro_until TIMESTAMPTZ;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ DEFAULT now();
        ALTER TABLE users ADD COLUMN IF NOT EXISTS lang TEXT DEFAULT 'uz';
        ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer BIGINT;
        ALTER TABLE downloads ADD COLUMN IF NOT EXISTS platform TEXT;
        ALTER TABLE downloads ADD COLUMN IF NOT EXISTS ok BOOLEAN DEFAULT TRUE;
        ALTER TABLE downloads ADD COLUMN IF NOT EXISTS err TEXT;
        ALTER TABLE downloads ADD COLUMN IF NOT EXISTS query TEXT;
        CREATE TABLE IF NOT EXISTS audio_cache(
            vid TEXT PRIMARY KEY, file_id TEXT, ts TIMESTAMPTZ DEFAULT now());
        ALTER TABLE audio_cache ADD COLUMN IF NOT EXISTS title TEXT;
        ALTER TABLE audio_cache ADD COLUMN IF NOT EXISTS dur INT;
        ALTER TABLE audio_cache ADD COLUMN IF NOT EXISTS plays INT DEFAULT 0;
        CREATE TABLE IF NOT EXISTS favorites(
            user_id BIGINT, vid TEXT, ts TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY(user_id, vid));
        CREATE TABLE IF NOT EXISTS promo(
            code TEXT PRIMARY KEY, days INT, max_uses INT DEFAULT 0,
            used INT DEFAULT 0, created TIMESTAMPTZ DEFAULT now());
        CREATE TABLE IF NOT EXISTS promo_used(
            code TEXT, user_id BIGINT, ts TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY(code, user_id));
        CREATE TABLE IF NOT EXISTS payments(
            id SERIAL PRIMARY KEY, user_id BIGINT, days INT, amount BIGINT,
            currency TEXT, charge_id TEXT, ts TIMESTAMPTZ DEFAULT now());
        CREATE TABLE IF NOT EXISTS scheduled(
            id SERIAL PRIMARY KEY, run_at TIMESTAMPTZ, admin_id BIGINT,
            chat_id BIGINT, msg_id BIGINT, done BOOLEAN DEFAULT FALSE);
        CREATE TABLE IF NOT EXISTS pay_cards(
            id SERIAL PRIMARY KEY, title TEXT, number TEXT, holder TEXT,
            active BOOLEAN DEFAULT TRUE);
        CREATE TABLE IF NOT EXISTS pay_plans(days INT PRIMARY KEY, price BIGINT);
        CREATE TABLE IF NOT EXISTS pay_requests(
            id SERIAL PRIMARY KEY, user_id BIGINT, days INT, price BIGINT, card_id INT,
            file_id TEXT, is_photo BOOLEAN DEFAULT TRUE, status TEXT DEFAULT 'pending',
            ts TIMESTAMPTZ DEFAULT now(), admin_id BIGINT, decided TIMESTAMPTZ);
        CREATE INDEX IF NOT EXISTS downloads_user_ts ON downloads(user_id, ts);
        CREATE INDEX IF NOT EXISTS users_referrer ON users(referrer);
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


def lt(ts, fmt="%d.%m %H:%M") -> str:
    return ts.astimezone(LOCAL).strftime(fmt) if ts else "—"


def ulabel(name, username) -> str:
    """HTML matn uchun: Ism @username"""
    return e(name) + (f" @{e(username)}" if username else "")


def ulabel_btn(name, username, n=22) -> str:
    """Tugma matni uchun (HTML emas)"""
    return (name or "?")[:n] + (f" @{username}" if username else "")


async def get_lang(uid: int) -> str:
    try:
        async with pool.acquire() as c:
            return (await c.fetchval("SELECT lang FROM users WHERE id=$1", uid)) or "uz"
    except Exception:
        return "uz"


async def grant_pro(uid: int, days: int):
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE users SET pro_until = GREATEST(now(), COALESCE(pro_until, now())) "
            "+ make_interval(days => $2) WHERE id=$1", uid, days)


async def notify_admins(text: str, key: str = "gen", every: int = 1200):
    """Asosiy adminlarga xabar (bir xil turdagi xabar `every` soniyada bir martadan ko'p emas)."""
    now = time.time()
    if now - ALERT_LAST.get(key, 0) < every:
        return
    ALERT_LAST[key] = now
    for aid in OWNERS:
        try:
            await bot.send_message(aid, text, parse_mode="HTML")
        except Exception:
            pass


def err_hint(s: str) -> str:
    low = s.lower().replace("\u2019", "'")
    if "not a bot" in low or "confirm you're" in low:
        return ("🚫 YouTube server IP sini bloklagan.\n"
                "➜ 🩺 Tizim → 🍪 Cookies ni yangilang yoki PROXY_URL qo'ying.")
    if ("player response" in low or "unable to extract" in low or "requested format" in low
            or "nsig" in low or "signature" in low):
        return ("⚠️ yt-dlp eskirgan yoki JS runtime (deno) yo'q.\n"
                "➜ 🩺 Tizim → 🔄 yt-dlp yangilash, so'ng ♻️ Qayta ishga tushirish.")
    if "ffmpeg" in low or "mp3 fayl yaratilmadi" in low:
        return "🎞 ffmpeg o'rnatilmagan yoki ishlamayapti."
    return ""


# ====================== LIMIT / PRO ======================
async def over_limit(uid: int) -> bool:
    if is_admin(uid):
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


def pro_text(lang: str) -> str:
    return CFG["pro_text"] if lang == "uz" else tr(lang, "pro_text")


def limit_text(lang: str) -> str:
    return tr(lang, "limit", n=CFG["daily_limit"], pro=pro_text(lang))


def start_text(lang: str) -> str:
    return CFG["start_text"] if lang == "uz" else tr(lang, "start")


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


async def send_gate(msg: Message, missing, lang: str = "uz"):
    rows = [[InlineKeyboardButton(text=f"📢 {ch['title']}", url=ch["link"])] for ch in missing]
    rows.append([InlineKeyboardButton(text=tr(lang, "check_btn"), callback_data="chk")])
    await msg.answer(tr(lang, "sub_gate"), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


class Gate(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = event.from_user
        msg = event if isinstance(event, Message) else event.message
        if not user or user.is_bot or msg is None or msg.chat.type != "private":
            return await handler(event, data)

        code = (user.language_code or "").lower()
        dflang = "ru" if code.startswith("ru") else "uz"
        async with pool.acquire() as c:
            row = await c.fetchrow(
                "INSERT INTO users(id,name,username,last_seen,lang) VALUES($1,$2,$3,now(),$4) "
                "ON CONFLICT(id) DO UPDATE SET name=$2, username=$3, active=TRUE, last_seen=now() "
                "RETURNING banned, lang, (xmax = 0) AS fresh",
                user.id, user.full_name, user.username, dflang)
        banned = row["banned"]
        lang = row["lang"] if row["lang"] in STR else "uz"
        data["lang"] = lang
        data["is_new"] = bool(row["fresh"])

        if is_admin(user.id):
            return await handler(event, data)

        async def deny(text):
            if isinstance(event, CallbackQuery):
                await event.answer(text[:190], show_alert=True)
            else:
                await event.answer(text, parse_mode="HTML")

        if banned:
            return await deny(tr(lang, "banned"))
        if CFG.get("maintenance") == "1":
            return await deny(CFG["maint_text"] if lang == "uz" else tr(lang, "maint"))

        is_chk = isinstance(event, CallbackQuery) and event.data == "chk"
        if CFG.get("sub_enabled") == "1" and not is_chk:
            missing = await unsubscribed(user.id)
            if missing:
                await send_gate(msg, missing, lang)
                if isinstance(event, CallbackQuery):
                    await event.answer()
                return
        return await handler(event, data)


dp.message.outer_middleware(Gate())
dp.callback_query.outer_middleware(Gate())


# ====================== YUKLASH FUNKSIYALARI ======================
def init_cookies():
    """Cookies: avval bazadagi (admin paneldan yuborilgan), bo'lmasa COOKIES_FILE."""
    data = (CFG.get("cookies_data") or "").strip()
    try:
        if data:
            with open(COOKIE_LOCAL, "w", encoding="utf-8") as f:
                f.write(data + "\n")
            os.chmod(COOKIE_LOCAL, 0o600)
        elif COOKIES and os.path.exists(COOKIES):
            shutil.copy(COOKIES, COOKIE_LOCAL)
    except Exception:
        logging.exception("init_cookies")


def base_opts():
    o = {"quiet": True, "noplaylist": True, "no_warnings": True, "socket_timeout": 15,
         "concurrent_fragment_downloads": 4,
         # xato bo'lsa uzoq qayta urinmaymiz (standart 10 marta — "qotib qolish" sababi)
         "retries": 2, "fragment_retries": 2, "extractor_retries": 1,
         # YouTube tezlikni cheklashini (throttle) chetlab o'tadi
         "http_chunk_size": 10 * 1024 * 1024}
    if os.path.exists(COOKIE_LOCAL):
        # har bir oqim o'z nusxasi bilan ishlaydi (yt-dlp cookie faylni qayta yozadi)
        local = f"/tmp/ck_{threading.get_ident()}.txt"
        try:
            shutil.copyfile(COOKIE_LOCAL, local)
            o["cookiefile"] = local
        except Exception:
            pass
    if PROXY:
        o["proxy"] = PROXY
    if not shutil.which("deno") and (shutil.which("node") or shutil.which("bun")):
        o["js_runtimes"] = {("node" if shutil.which("node") else "bun"): {}}
    return o


def fmt_dur(sec):
    if not sec:
        return ""
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


def search_music(query: str):
    opts = base_opts() | {"extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"ytsearch{SEARCH_N}:{query}", download=False)
    return [
        {"id": x["id"], "title": x.get("title") or "?", "dur": x.get("duration")}
        for x in info.get("entries", []) if x
    ]


async def search_cached(q: str):
    k = q.lower().strip()
    hit = SEARCH_CACHE.get(k)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    res = await run(search_music, q)
    if res:
        SEARCH_CACHE[k] = (time.time(), res)
        if len(SEARCH_CACHE) > 300:
            SEARCH_CACHE.pop(next(iter(SEARCH_CACHE)))
    return res


YT_ATTEMPTS = [
    None,
    {"player_client": ["tv", "web_safari"]},
    {"player_client": ["ios"]},
    {"player_client": ["android_vr"]},
    {"player_client": ["mweb"]},
    {"player_client": ["web_embedded"]},
    {"player_client": ["tv_simply", "android_vr"]},
]
_YT_BEST = {"i": 0}  # oxirgi ishlagan klient: keyingi qo'shiqda birinchi shuni sinaymiz
_YT_BLOCK = {"until": 0.0}  # YouTube IP ni bloklagan bo'lsa, shu vaqtgacha uni qayta-qayta urmaymiz


def _download_audio(url: str, folder: str, yt_args=None, quality=None):
    opts = base_opts() | {
        # 140 = 128 kbps m4a: eng kichik va tez; kerak bo'lmasa keyingilariga o'tadi
        "format": ("bestaudio/best" if quality else "140/bestaudio[ext=m4a]/bestaudio/best"),
        "outtmpl": f"{folder}/%(id)s.%(ext)s",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": quality or AUDIO_QUALITY,
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
        if dur and d and abs(d - dur) > max(25, dur * 0.15):
            continue
        urls.append(u)
    return urls[:6]


def yt_alternatives(q: str, dur=None, skip_id=None):
    """Shu qo'shiqning boshqa YouTube yuklamalari (davomiyligi mos kelganlari)."""
    opts = base_opts() | {"extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"ytsearch8:{q}", download=False)
    out = []
    for x in info.get("entries", []):
        if not x or x.get("id") == skip_id:
            continue
        d = x.get("duration")
        if dur and d and abs(d - dur) > max(25, dur * 0.15):
            continue
        out.append(f"https://www.youtube.com/watch?v={x['id']}")
    return out[:3]


def _is_block(s: str) -> bool:
    s = s.lower().replace("\u2019", "'")
    return "not a bot" in s or "confirm you're" in s


def download_audio(url: str, folder: str, fallback: str = None, dur=None, quality=None):
    t0 = time.monotonic()

    def left():
        return DL_BUDGET - (time.monotonic() - t0)

    yt_errs, sc_err, n_block = [], None, 0
    is_yt = "youtube.com" in url or "youtu.be" in url
    if is_yt:
        i0 = _YT_BEST["i"] % len(YT_ATTEMPTS)
        order = list(range(i0, len(YT_ATTEMPTS))) + list(range(i0))
        if fallback and time.time() < _YT_BLOCK["until"]:
            order = order[:1]  # IP yaqinda bloklangan: bitta urinish, keyin darrov SoundCloud
    else:
        order = [None]
    for idx in order:
        if left() <= 0:
            break
        args = YT_ATTEMPTS[idx] if idx is not None else None
        try:
            res = _download_audio(url, folder, args, quality)
            if idx is not None:
                _YT_BEST["i"] = idx
                _YT_BLOCK["until"] = 0.0
            return res
        except Exception as ex:
            s = short_err(ex)
            yt_errs.append(s)
            logging.warning("audio urinish xato (%s): %s", args, s)
            if _is_block(s):
                n_block += 1
                if n_block >= 2:  # bu IP darajasidagi blok: boshqa klientlar ham yordam bermaydi
                    break
    blocked = n_block >= 2 or (n_block >= 1 and len(order) == 1)
    if is_yt and blocked:
        _YT_BLOCK["until"] = time.time() + 300
    if is_yt and fallback and not blocked and left() > 20:
        m_id = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", url)
        try:
            for alt in yt_alternatives(fallback, dur, m_id.group(1) if m_id else None):
                for args in (None, {"player_client": ["ios"]}):
                    try:
                        return _download_audio(alt, folder, args, quality)
                    except Exception as ex:
                        logging.warning("muqobil yuklama xato (%s): %s", alt, short_err(ex))
        except Exception as ex:
            logging.warning("muqobil qidiruv xato: %s", short_err(ex))
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
                if u in seen or tries >= 5 or left() <= 0:
                    continue
                seen.add(u)
                tries += 1
                try:
                    path, info = _download_audio(u, folder, None, quality)
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
    first = yt_errs[0] if yt_errs else "noma'lum xato"
    msg = f"{'YouTube' if is_yt else 'Yuklash'}: {first[:230]}"
    if sc_err:
        msg += f" | SoundCloud: {sc_err[:220]}"
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


def to_wav(src: str, dst: str):
    """Ovozni mono WAV ga o'giradi (birinchi 30 soniya)."""
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-t", "30", "-vn", "-ac", "1", "-ar", "44100", dst],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def cut_wav(src: str, dst: str, start: int, length: int = 12):
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(start), "-t", str(length), "-i", src,
         "-ac", "1", "-ar", "44100", dst],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


async def shazam_recognize(path: str):
    """(title, artist) yoki None qaytaradi."""
    if Shazam is None:
        raise RuntimeError("shazamio o'rnatilmagan (requirements.txt ga qo'shing)")
    sh = Shazam()
    fn = getattr(sh, "recognize", None) or getattr(sh, "recognize_song")
    out = await asyncio.wait_for(fn(path), timeout=45)
    track = (out or {}).get("track")
    if not track:
        return None
    return track.get("title") or "", track.get("subtitle") or ""


async def audd_recognize(path: str):
    """Ixtiyoriy zaxira: AUDD_TOKEN berilgan bo'lsa."""
    if not AUDD_TOKEN:
        return None
    with open(path, "rb") as f:
        raw = f.read()
    form = aiohttp.FormData()
    form.add_field("api_token", AUDD_TOKEN)
    form.add_field("file", raw, filename="a.wav", content_type="audio/wav")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
        async with s.post("https://api.audd.io/", data=form) as r:
            j = await r.json(content_type=None)
    res = (j or {}).get("result")
    if res:
        return res.get("title") or "", res.get("artist") or ""
    return None


async def recognize_any(wav: str, tmp: str):
    """Ovozning bir necha qismini (0-12s, 8-20s, 16-28s) Shazam bilan sinaydi, keyin AudD."""
    last_exc, any_ok = None, False
    for i, start in enumerate((0, 8, 16)):
        part = f"{tmp}/p{i}.wav"
        try:
            await run(cut_wav, wav, part, start)
        except Exception:
            break
        if not os.path.exists(part) or os.path.getsize(part) < 200_000:
            break
        try:
            found = await shazam_recognize(part)
            any_ok = True
            if found and (found[0] or found[1]):
                return found
        except Exception as ex:
            last_exc = ex
            logging.warning("shazam xato (%ss): %s", start, short_err(ex))
    try:
        found = await audd_recognize(wav)
        if found:
            return found
    except Exception as ex:
        logging.warning("audd xato: %s", short_err(ex))
    if last_exc and not any_ok:
        raise last_exc
    return None


# ====================== QIDIRUV NATIJALARI (SAHIFALASH) ======================
PAGE = 10


def render_page(sid: str, page: int):
    d = SEARCHES.get(sid)
    if not d:
        return None
    items = d["items"]
    pages = max(1, (len(items) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * PAGE:(page + 1) * PAGE]
    lines = [d["head"] + "\n"]
    for i, r in enumerate(chunk, 1):
        lines.append(f"<b>{i}.</b> {e(r['title'])} <b>{fmt_dur(r['dur'])}</b>")
    nums = [InlineKeyboardButton(text=str(i), callback_data=f"s:{sid}:{page * PAGE + i - 1}")
            for i in range(1, len(chunk) + 1)]
    rows = [nums[i:i + 5] for i in range(0, len(nums), 5)]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"p:{sid}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"p:{sid}:{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="❌", callback_data="x")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def send_results(m: Message, results, head: str):
    sid = uuid.uuid4().hex[:8]
    remember(SEARCHES, sid, {"items": results, "head": head})
    text, markup = render_page(sid, 0)
    await m.answer(text, parse_mode="HTML", reply_markup=markup)


# ====================== ODDIY FOYDALANUVCHI ======================
async def handle_ref(uid: int, raw: str):
    if not raw.isdigit():
        return
    rid = int(raw)
    if rid == uid:
        return
    async with pool.acquire() as c:
        ok = await c.fetchval(
            "UPDATE users SET referrer=$1 WHERE id=$2 AND referrer IS NULL "
            "AND EXISTS (SELECT 1 FROM users WHERE id=$1) RETURNING 1", rid, uid)
    if not ok:
        return
    days = int(CFG.get("ref_days") or 0)
    if days > 0:
        await grant_pro(rid, days)
        try:
            await bot.send_message(rid, tr(await get_lang(rid), "ref_new", days=days))
        except Exception:
            pass


@main.message(CommandStart())
async def start(m: Message, state: FSMContext, command: CommandObject,
                lang: str = "uz", is_new: bool = False):
    await state.clear()
    uid = m.from_user.id
    payload = (command.args or "").strip()
    if payload.startswith("ref_") and is_new:
        try:
            await handle_ref(uid, payload[4:])
        except Exception:
            logging.exception("ref")
    await m.answer(start_text(lang), parse_mode="HTML", reply_markup=user_kb(uid, lang))
    if payload.startswith("s_") and YT_ID_OK.fullmatch(payload[2:]):
        # inline rejimdan kelgan: qo'shiqni darrov yuklaymiz
        vid = payload[2:]
        if CFG["feat_music"] != "1" and not is_admin(uid):
            return await m.answer(tr(lang, "off"))
        if await over_limit(uid):
            return await m.answer(limit_text(lang), parse_mode="HTML")
        meta = INLINE_META.get(vid) or (None, None)
        await send_mp3(m, uid, lang, f"https://www.youtube.com/watch?v={vid}", meta[0], meta[1])


@main.message(Command("fav"))
async def cmd_fav(m: Message, lang: str = "uz"):
    await show_fav(m, m.from_user.id, lang)


@main.message(Command("top"))
async def cmd_top(m: Message, lang: str = "uz"):
    await show_top(m, lang)


@main.message(Command("ref"))
async def cmd_ref(m: Message, lang: str = "uz"):
    await show_ref(m, m.from_user.id, lang)


@main.message(Command("lang"))
async def cmd_lang(m: Message, lang: str = "uz"):
    await show_pick_lang(m, lang)


@main.message(Command("buy"))
async def cmd_buy(m: Message, lang: str = "uz"):
    await show_buy(m, lang)


@main.message(Command("promo"))
async def cmd_promo(m: Message, command: CommandObject, lang: str = "uz"):
    code = (command.args or "").strip()
    if not code:
        return await m.answer(tr(lang, "promo_usage"))
    res, days = await redeem(code, m.from_user.id)
    if res == "ok":
        await m.answer(tr(lang, "promo_ok", days=days))
    elif res == "used":
        await m.answer(tr(lang, "promo_used"))
    else:
        await m.answer(tr(lang, "promo_bad"))


@main.callback_query(F.data == "chk")
async def check_sub(call: CallbackQuery, lang: str = "uz"):
    missing = await unsubscribed(call.from_user.id)
    if missing:
        return await call.answer(tr(lang, "sub_missing"), show_alert=True)
    await call.answer(tr(lang, "thanks"))
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(start_text(lang), parse_mode="HTML",
                              reply_markup=user_kb(call.from_user.id, lang))


@main.callback_query(F.data == "noop")
async def noop(call: CallbackQuery):
    await call.answer()


# ---- referal / til / PRO / promo / to'lov ----
async def show_ref(m: Message, uid: int, lang: str):
    async with pool.acquire() as c:
        n = await c.fetchval("SELECT count(*) FROM users WHERE referrer=$1", uid)
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    await m.answer(tr(lang, "ref_text", link=link, days=CFG.get("ref_days") or "0", n=n),
                   parse_mode="HTML", disable_web_page_preview=True)


async def show_pick_lang(m: Message, lang: str):
    await m.answer(tr(lang, "lang_pick"), reply_markup=kb([[
        ("🇺🇿 O'zbekcha", "lg:uz"), ("🇷🇺 Русский", "lg:ru"), ("🇬🇧 English", "lg:en")]]))


@main.callback_query(F.data.startswith("lg:"))
async def set_lang(call: CallbackQuery):
    code = call.data[3:]
    if code not in STR:
        return await call.answer()
    async with pool.acquire() as c:
        await c.execute("UPDATE users SET lang=$2 WHERE id=$1", call.from_user.id, code)
    await call.answer(tr(code, "lang_set"))
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(start_text(code), parse_mode="HTML",
                              reply_markup=user_kb(call.from_user.id, code))


class Receipt(StatesGroup):
    wait = State()


def money(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def fmt_card(num: str) -> str:
    d = re.sub(r"\D", "", num or "")
    return " ".join(d[i:i + 4] for i in range(0, len(d), 4))


async def get_plans() -> dict:
    """Tariflar: avval bazadagi (admin panel), bo'lmasa PRO_PLANS env."""
    try:
        async with pool.acquire() as c:
            rows = await c.fetch("SELECT days, price FROM pay_plans ORDER BY days")
        if rows:
            return {r["days"]: r["price"] for r in rows}
    except Exception:
        logging.exception("get_plans")
    return dict(PRO_PLANS)


async def get_cards():
    async with pool.acquire() as c:
        return await c.fetch("SELECT id, title, number, holder FROM pay_cards WHERE active ORDER BY id")


async def show_buy(m: Message, lang: str):
    plans = await get_plans()
    cards = await get_cards()
    if not plans or not (PAY_TOKEN or cards):
        return await m.answer(tr(lang, "buy_none", pro=pro_text(lang)), parse_mode="HTML")
    rows = [[(tr(lang, "price_fmt", days=d, price=money(p)), f"buy:{d}")]
            for d, p in sorted(plans.items())]
    await m.answer(tr(lang, "buy_pick"), parse_mode="HTML", reply_markup=kb(rows))


async def send_invoice_for(uid: int, lang: str, days: int, price: int):
    await bot.send_invoice(
        uid,
        title=tr(lang, "buy_title", days=days),
        description=tr(lang, "buy_desc", days=days),
        payload=f"pro:{days}",
        provider_token=PAY_TOKEN,
        currency=PAY_CURRENCY,
        prices=[LabeledPrice(label=f"PRO {days}", amount=price * 100)],  # eng kichik birlikda (tiyin)
    )


async def start_manual(msg: Message, state: FSMContext, lang: str, days: int, price: int):
    cards = await get_cards()
    if not cards:
        return await msg.answer(tr(lang, "off"))
    lines = []
    for c in cards:
        t = f"💳 <code>{e(fmt_card(c['number']))}</code>\n👤 {e(c['holder'])}"
        if c["title"]:
            t += f" ({e(c['title'])})"
        lines.append(t)
    await state.set_state(Receipt.wait)
    await state.update_data(days=days, price=int(price), card_id=cards[0]["id"])
    await msg.answer(tr(lang, "pay_card", days=days, price=money(price), cards="\n\n".join(lines)),
                     parse_mode="HTML")


@main.callback_query(F.data.startswith("buy:"))
async def buy_cb(call: CallbackQuery, state: FSMContext, lang: str = "uz"):
    try:
        days = int(call.data.split(":")[1])
    except ValueError:
        return await call.answer()
    price = (await get_plans()).get(days)
    cards = await get_cards()
    if not price or not (PAY_TOKEN or cards):
        return await call.answer(tr(lang, "off"), show_alert=True)
    await call.answer()
    if PAY_TOKEN and cards:
        return await call.message.answer(tr(lang, "buy_method"), reply_markup=kb([
            [(tr(lang, "m_auto"), f"bi:{days}")],
            [(tr(lang, "m_card"), f"bm:{days}")],
        ]))
    if PAY_TOKEN:
        await send_invoice_for(call.from_user.id, lang, days, price)
    else:
        await start_manual(call.message, state, lang, days, price)


@main.callback_query(F.data.startswith("bi:") | F.data.startswith("bm:"))
async def buy_method(call: CallbackQuery, state: FSMContext, lang: str = "uz"):
    kind, d = call.data.split(":")
    days = int(d)
    price = (await get_plans()).get(days)
    if not price:
        return await call.answer(tr(lang, "off"), show_alert=True)
    await call.answer()
    if kind == "bi" and PAY_TOKEN:
        await send_invoice_for(call.from_user.id, lang, days, price)
    else:
        await start_manual(call.message, state, lang, days, price)


@main.message(Command("cancel"))
async def user_cancel(m: Message, state: FSMContext, lang: str = "uz"):
    await state.clear()
    await m.answer(tr(lang, "cancelled"), reply_markup=user_kb(m.from_user.id, lang))


async def send_receipt_to_admins(rid, uid, name, username, days, price, file_id, is_photo):
    caption = (f"🧾 Yangi chek #{rid}\n👤 {name or '?'}" + (f" @{username}" if username else "") +
               f" ({uid})\n💎 {days} kun — {money(price)} so'm")
    markup = kb([[("✅ Tasdiqlash", f"pr:ok:{rid}"), ("❌ Rad etish", f"pr:no:{rid}")]])
    for aid in (OWNERS | ADMIN_SET):
        try:
            if is_photo:
                await bot.send_photo(aid, file_id, caption=caption, reply_markup=markup)
            else:
                await bot.send_document(aid, file_id, caption=caption, reply_markup=markup)
        except Exception:
            logging.warning("chek adminga yuborilmadi: %s", aid)


@main.message(Receipt.wait, F.photo | F.document)
async def got_receipt(m: Message, state: FSMContext, lang: str = "uz"):
    data = await state.get_data()
    days, price = data.get("days"), data.get("price")
    if not days or not price:
        await state.clear()
        return
    uid = m.from_user.id
    is_photo = bool(m.photo)
    file_id = m.photo[-1].file_id if is_photo else m.document.file_id
    async with pool.acquire() as c:
        pend = await c.fetchval(
            "SELECT count(*) FROM pay_requests WHERE user_id=$1 AND status='pending'", uid)
        if pend >= 3:
            return await m.answer(tr(lang, "chek_limit"))
        rid = await c.fetchval(
            "INSERT INTO pay_requests(user_id, days, price, card_id, file_id, is_photo) "
            "VALUES($1,$2,$3,$4,$5,$6) RETURNING id",
            uid, days, price, data.get("card_id"), file_id, is_photo)
    await state.clear()
    await m.answer(tr(lang, "chek_sent"), reply_markup=user_kb(uid, lang))
    await send_receipt_to_admins(rid, uid, m.from_user.full_name, m.from_user.username,
                                 days, price, file_id, is_photo)


@main.message(Receipt.wait, F.text, ~F.text.startswith("/"))
async def receipt_text(m: Message, lang: str = "uz"):
    await m.answer(tr(lang, "chek_need"))


@main.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@main.message(F.successful_payment)
async def paid(m: Message, lang: str = "uz"):
    sp = m.successful_payment
    uid = m.from_user.id
    try:
        days = int(sp.invoice_payload.split(":")[1])
    except Exception:
        return
    await grant_pro(uid, days)
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO payments(user_id, days, amount, currency, charge_id) VALUES($1,$2,$3,$4,$5)",
            uid, days, sp.total_amount, sp.currency, sp.provider_payment_charge_id)
    await m.answer(tr(lang, "paid", days=days))
    await notify_admins(
        f"💰 <b>Yangi to'lov</b>\n{ulabel(m.from_user.full_name, m.from_user.username)} "
        f"(<code>{uid}</code>)\n{days} kun — {sp.total_amount / 100:,.0f} {sp.currency}",
        key=f"pay{uid}{time.time()}", every=0)


async def redeem(code: str, uid: int):
    code = code.strip().upper()
    async with pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT days, max_uses, used FROM promo WHERE code=$1 FOR UPDATE", code)
            if not p or (p["max_uses"] and p["max_uses"] > 0 and p["used"] >= p["max_uses"]):
                return "bad", 0
            ok = await c.fetchval(
                "INSERT INTO promo_used(code, user_id) VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING 1",
                code, uid)
            if not ok:
                return "used", 0
            await c.execute("UPDATE promo SET used=used+1 WHERE code=$1", code)
    await grant_pro(uid, p["days"])
    return "ok", p["days"]


# ---- sevimlilar / top ----
async def fav_markup(uid: int):
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT f.vid, COALESCE(a.title, f.vid) AS title FROM favorites f "
            "LEFT JOIN audio_cache a ON a.vid = f.vid WHERE f.user_id=$1 "
            "ORDER BY f.ts DESC LIMIT 20", uid)
    if not rows:
        return None
    return kb([[(r["title"][:45], f"g:{r['vid']}"), ("🗑", f"fr:{r['vid']}")] for r in rows])


async def show_fav(m: Message, uid: int, lang: str):
    markup = await fav_markup(uid)
    if not markup:
        return await m.answer(tr(lang, "fav_empty"))
    await m.answer(tr(lang, "fav_title"), parse_mode="HTML", reply_markup=markup)


@main.callback_query(F.data.startswith("fav:"))
async def fav_add(call: CallbackQuery, lang: str = "uz"):
    vid = call.data[4:]
    if not YT_ID_OK.fullmatch(vid):
        return await call.answer()
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO favorites(user_id, vid) VALUES($1,$2) ON CONFLICT DO NOTHING",
            call.from_user.id, vid)
    await call.answer(tr(lang, "fav_saved"))


@main.callback_query(F.data.startswith("fr:"))
async def fav_remove(call: CallbackQuery, lang: str = "uz"):
    vid = call.data[3:]
    uid = call.from_user.id
    async with pool.acquire() as c:
        await c.execute("DELETE FROM favorites WHERE user_id=$1 AND vid=$2", uid, vid)
    markup = await fav_markup(uid)
    try:
        if markup:
            await call.message.edit_reply_markup(reply_markup=markup)
        else:
            await call.message.edit_text(tr(lang, "fav_empty"))
    except TelegramBadRequest:
        pass
    await call.answer(tr(lang, "fav_removed"))


async def show_top(m: Message, lang: str):
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT vid, title, dur FROM audio_cache WHERE title IS NOT NULL AND plays > 0 "
            "ORDER BY plays DESC, ts DESC LIMIT 10")
    if not rows:
        return await m.answer(tr(lang, "top_empty"))
    lines = [tr(lang, "top_title"), ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"<b>{i}.</b> {e(r['title'])} <b>{fmt_dur(r['dur'])}</b>")
    nums = [InlineKeyboardButton(text=str(i), callback_data=f"g:{r['vid']}")
            for i, r in enumerate(rows, 1)]
    rws = [nums[i:i + 5] for i in range(0, len(nums), 5)]
    await m.answer("\n".join(lines), parse_mode="HTML",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rws))


@main.callback_query(F.data.startswith("g:"))
async def get_by_vid(call: CallbackQuery, lang: str = "uz"):
    vid = call.data[2:]
    uid = call.from_user.id
    if not YT_ID_OK.fullmatch(vid):
        return await call.answer()
    if await over_limit(uid):
        await call.answer()
        return await call.message.answer(limit_text(lang), parse_mode="HTML")
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT title, dur FROM audio_cache WHERE vid=$1", vid)
    await call.answer(tr(lang, "loading"))
    await send_mp3(call.message, uid, lang, f"https://www.youtube.com/watch?v={vid}",
                   row["title"] if row else None, row["dur"] if row else None)


# ---- video -> dumaloq ----
@main.message(F.video)
async def to_round(m: Message, lang: str = "uz"):
    uid = m.from_user.id
    if CFG["feat_round"] != "1" and not is_admin(uid):
        return await m.answer(tr(lang, "off"))
    if await over_limit(uid):
        return await m.answer(limit_text(lang), parse_mode="HTML")
    if m.video.file_size and m.video.file_size > 20 * 1024 * 1024:
        return await m.answer(tr(lang, "round_big"))
    wait = await m.answer(tr(lang, "round_wait"))
    tmp = tempfile.mkdtemp()
    try:
        src, dst = f"{tmp}/in.mp4", f"{tmp}/round.mp4"
        await bot.download(m.video, destination=src)
        async with DL_SEM:
            await run(make_round, src, dst)
        await m.answer_video_note(FSInputFile(dst), length=640)
        await log_dl(uid, "round", "telegram")
    except Exception as ex:
        logging.exception("round error")
        await log_dl(uid, "round", "telegram", ok=False, err=short_err(ex))
        await m.answer(tr(lang, "round_fail"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            await wait.delete()
        except Exception:
            pass


# ---- ovoz orqali qo'shiq topish ----
@main.message(F.voice | F.audio | F.video_note)
async def voice_handler(m: Message, lang: str = "uz"):
    uid = m.from_user.id
    if CFG["feat_voice"] != "1" and not is_admin(uid):
        return await m.answer(tr(lang, "off"))
    media = m.voice or m.audio or m.video_note
    if media.file_size and media.file_size > 20 * 1024 * 1024:
        return await m.answer(tr(lang, "file_big20"))
    wait = await m.answer(tr(lang, "listening"))
    tmp = tempfile.mkdtemp()
    try:
        src, wav = f"{tmp}/in.bin", f"{tmp}/in.wav"
        await bot.download(media, destination=src)
        await run(to_wav, src, wav)
        found = await recognize_any(wav, tmp)
        if not found or not (found[0] or found[1]):
            await log_dl(uid, "voice", "shazam", ok=False, err="aniqlanmadi")
            return await m.answer(tr(lang, "not_recognized"))
        title, artist = found
        query = f"{artist} - {title}".strip(" -")
        await log_dl(uid, "voice", "shazam", query=query[:200])
        try:
            results = await search_cached(query)
        except Exception:
            logging.exception("voice search error")
            results = []
        head = f"🎤 <b>{e(artist)} — {e(title)}</b>"
        if not results:
            return await m.answer(tr(lang, "recog_nores", head=head), parse_mode="HTML")
        await send_results(m, results, head)
    except Exception as ex:
        logging.exception("voice error")
        err = short_err(ex)
        await log_dl(uid, "voice", "shazam", ok=False, err=err)
        msg = tr(lang, "recog_fail")
        if is_admin(uid):
            msg += f"\n\n<code>{e(err)}</code>"
        await m.answer(msg, parse_mode="HTML")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            await wait.delete()
        except Exception:
            pass


# ---- matn: tugmalar / havola / qidiruv ----
@main.message(F.text, ~F.text.startswith("/"))
async def text_handler(m: Message, lang: str = "uz"):
    uid = m.from_user.id
    text = m.text.strip()

    act = BTN_REV.get(text)
    if act == "fav":
        return await show_fav(m, uid, lang)
    if act == "top":
        return await show_top(m, lang)
    if act == "ref":
        return await show_ref(m, uid, lang)
    if act == "pro":
        return await show_buy(m, lang)
    if act == "lang":
        return await show_pick_lang(m, lang)

    link = URL_RE.search(text)
    if link:
        if CFG["feat_video"] != "1" and not is_admin(uid):
            return await m.answer(tr(lang, "off"))
        url = link.group(0)
        if not any(d in url for d in SUPPORTED):
            return await m.answer(tr(lang, "only_sup"))
        if await over_limit(uid):
            return await m.answer(limit_text(lang), parse_mode="HTML")
        if uid in BUSY:
            return await m.answer(tr(lang, "busy"))
        BUSY.add(uid)
        wait = await m.answer(tr(lang, "wait_dl"))
        tmp = tempfile.mkdtemp()
        try:
            async with DL_SEM:
                path, info = await run(download_video, url, tmp)
            if os.path.getsize(path) > MAX_SIZE:
                return await m.answer(tr(lang, "video_big"))
            lid = uuid.uuid4().hex[:8]
            remember(LINKS, lid, url)
            await m.answer_video(
                FSInputFile(path),
                caption=CFG["caption"].replace("{bot}", BOT_USERNAME),
                reply_markup=kb([[(tr(lang, "dl_audio_btn"), f"a:{lid}")]]),
            )
            await log_dl(uid, "video", platform_of(url))
        except Exception as ex:
            logging.exception("video error")
            err = short_err(ex)
            await log_dl(uid, "video", platform_of(url), ok=False, err=err)
            msg = tr(lang, "video_fail")
            if is_admin(uid):
                msg += f"\n\n<code>{e(err)}</code>"
                hint = err_hint(err)
                if hint:
                    msg += f"\n\n{e(hint)}"
            else:
                hint = err_hint(err)
                if hint:
                    await notify_admins(f"⚠️ <b>Video yuklashda xato</b>\n{e(hint)}\n\n<code>{e(err)}</code>",
                                        key="vid:" + hint[:2])
            await m.answer(msg, parse_mode="HTML")
        finally:
            BUSY.discard(uid)
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                await wait.delete()
            except Exception:
                pass
        return

    if CFG["feat_music"] != "1" and not is_admin(uid):
        return await m.answer(tr(lang, "off"))
    wait = await m.answer(tr(lang, "searching"))
    try:
        results = await search_cached(text)
    except Exception as ex:
        logging.exception("search error")
        await log_dl(uid, "search", "youtube", ok=False, err=short_err(ex), query=text[:200])
        results = []
    try:
        await wait.delete()
    except Exception:
        pass
    if not results:
        return await m.answer(tr(lang, "nothing"))
    await log_dl(uid, "search", "youtube", query=text[:200])
    await send_results(m, results, f"🔍 <i>{e(text)}</i>")


# ---- inline rejim: @bot qo'shiq nomi (BotFather → /setinline ni yoqing) ----
@main.inline_query()
async def inline_search(q: InlineQuery):
    text = (q.query or "").strip()
    if len(text) < 2:
        return await q.answer([], cache_time=5, is_personal=False)
    try:
        results = await search_cached(text)
    except Exception:
        logging.exception("inline search")
        return await q.answer([], cache_time=5)
    items = []
    for r in results[:10]:
        remember(INLINE_META, r["id"], (r["title"], r["dur"]))
        items.append(InlineQueryResultArticle(
            id=r["id"],
            title=r["title"][:100],
            description=fmt_dur(r["dur"]) or "🎵",
            input_message_content=InputTextMessageContent(
                message_text=f"🎵 <b>{e(r['title'])}</b>", parse_mode="HTML"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="⬇️ MP3", url=f"https://t.me/{BOT_USERNAME}?start=s_{r['id']}")]]),
        ))
    await q.answer(items, cache_time=300, is_personal=False)


# ---- MP3 yuborish ----
async def cache_get(vid):
    try:
        async with pool.acquire() as c:
            return await c.fetchval("SELECT file_id FROM audio_cache WHERE vid=$1", vid)
    except Exception:
        logging.exception("cache_get")
        return None


async def cache_put(vid, file_id, title=None, dur=None):
    try:
        async with pool.acquire() as c:
            await c.execute(
                "INSERT INTO audio_cache(vid, file_id, title, dur, plays) VALUES($1,$2,$3,$4,1) "
                "ON CONFLICT(vid) DO UPDATE SET file_id=$2, ts=now(), "
                "title=COALESCE($3, audio_cache.title), dur=COALESCE($4, audio_cache.dur), "
                "plays=COALESCE(audio_cache.plays,0)+1", vid, file_id, title, dur)
    except Exception:
        logging.exception("cache_put")


async def cache_bump(vid):
    try:
        async with pool.acquire() as c:
            await c.execute("UPDATE audio_cache SET plays=COALESCE(plays,0)+1 WHERE vid=$1", vid)
    except Exception:
        logging.exception("cache_bump")


async def cache_del(vid):
    try:
        async with pool.acquire() as c:
            await c.execute("DELETE FROM audio_cache WHERE vid=$1", vid)
    except Exception:
        logging.exception("cache_del")


async def send_mp3(msg: Message, uid: int, lang: str, url: str, fallback: str = None, dur=None):
    m_id = YT_ID_RE.search(url)
    vid = m_id.group(1) if m_id else None
    markup = kb([[(tr(lang, "fav_btn"), f"fav:{vid}")]]) if vid else None

    pro_q = PRO_QUALITY if (not is_admin(uid) and await is_pro(uid)) or is_admin(uid) else None
    if pro_q == AUDIO_QUALITY:
        pro_q = None

    # Oldin yuklangan qo'shiq bo'lsa — darrov file_id bilan yuboramiz (PRO yuqori sifat keshlanmaydi)
    if vid and not pro_q:
        fid = await cache_get(vid)
        if fid:
            try:
                await msg.answer_audio(fid, caption=f"🎵 @{BOT_USERNAME}", reply_markup=markup)
                await cache_bump(vid)
                await log_dl(uid, "music", "cache", query=(fallback or "")[:200])
                return
            except Exception:
                logging.warning("cache file_id ishlamadi, qayta yuklanadi")
                await cache_del(vid)

    if uid in BUSY:
        return await msg.answer(tr(lang, "busy"))
    BUSY.add(uid)
    wait = await msg.answer(tr(lang, "wait_mp3"))
    tmp = tempfile.mkdtemp()
    try:
        async with DL_SEM:
            path, info = await run(download_audio, url, tmp, fallback, dur, pro_q)
        if os.path.getsize(path) > MAX_SIZE:
            return await msg.answer(tr(lang, "file_big"))
        sent = await msg.answer_audio(
            FSInputFile(path),
            title=info.get("title"),
            performer=info.get("uploader"),
            duration=int(round(float(info.get("duration") or 0))) or None,
            caption=f"🎵 @{BOT_USERNAME}",
            reply_markup=markup,
        )
        if vid and sent.audio and not pro_q:
            await cache_put(vid, sent.audio.file_id, fallback or info.get("title"),
                            dur or (int(info["duration"]) if info.get("duration") else None))
        src = (info.get("extractor_key") or platform_of(url)).lower()
        await log_dl(uid, "music", src, query=(fallback or info.get("title") or "")[:200])
    except Exception as ex:
        logging.exception("audio error")
        err = short_err(ex)
        await log_dl(uid, "music", platform_of(url), ok=False, err=err, query=(fallback or "")[:200])
        hint = err_hint(err)
        text = tr(lang, "mp3_fail")
        if is_admin(uid):
            text += f"\n\n<code>{e(err)}</code>"
            if hint:
                text += f"\n\n{e(hint)}"
        await msg.answer(text, parse_mode="HTML")
        if uid not in OWNERS:
            await notify_admins(
                f"⚠️ <b>MP3 yuklashda xato</b>\n{e(hint)}\n\n<code>{e(err)}</code>",
                key="mp3:" + (hint[:2] or "x"))
    finally:
        BUSY.discard(uid)
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            await wait.delete()
        except Exception:
            pass


@main.callback_query(F.data.startswith("s:"))
async def pick_song(call: CallbackQuery, lang: str = "uz"):
    _, sid, idx = call.data.split(":")
    d = SEARCHES.get(sid)
    if not d:
        return await call.answer(tr(lang, "stale_search"), show_alert=True)
    uid = call.from_user.id
    if await over_limit(uid):
        await call.answer()
        return await call.message.answer(limit_text(lang), parse_mode="HTML")
    await call.answer(tr(lang, "loading"))
    song = d["items"][int(idx)]
    await send_mp3(call.message, uid, lang, f"https://www.youtube.com/watch?v={song['id']}",
                   song["title"], song["dur"])


@main.callback_query(F.data.startswith("p:"))
async def page_cb(call: CallbackQuery, lang: str = "uz"):
    _, sid, pg = call.data.split(":")
    r = render_page(sid, int(pg))
    if not r:
        return await call.answer(tr(lang, "stale_search"), show_alert=True)
    try:
        await call.message.edit_text(r[0], parse_mode="HTML", reply_markup=r[1])
    except TelegramBadRequest:
        pass
    await call.answer()


@main.callback_query(F.data.startswith("a:"))
async def audio_from_link(call: CallbackQuery, lang: str = "uz"):
    url = LINKS.get(call.data[2:])
    if not url:
        return await call.answer(tr(lang, "stale_link"), show_alert=True)
    uid = call.from_user.id
    if await over_limit(uid):
        await call.answer()
        return await call.message.answer(limit_text(lang), parse_mode="HTML")
    await call.answer(tr(lang, "loading"))
    await send_mp3(call.message, uid, lang, url)


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
    cookies = State()
    promo = State()
    dm = State()
    card = State()
    plan = State()


def menu():
    mt = "✅ YOQILGAN" if CFG["maintenance"] == "1" else "❌ o'chiq"
    return kb([
        [("📊 Statistika", "ad:st"), ("👥 Userlar", "ad:us")],
        [("📢 Xabarlar", "ad:ms"), ("🔗 Majburiy obuna", "ad:ch")],
        [("💎 PRO / Promo / To'lov", "ad:pm"), ("⚙️ Sozlamalar / Limit", "ad:set")],
        [("🩺 Tizim", "ad:sys"), ("👮 Adminlar", "ad:adm")],
        [(f"🛠 Maintenance: {mt}", "ad:tg:maintenance:home")],
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
    await m.answer(PANEL_TITLE, reply_markup=menu(), parse_mode="HTML")


@admin.message(Command("cancel"))
async def cancel_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=menu())


@admin.callback_query(F.data == "ad:home")
async def ad_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, PANEL_TITLE, menu())


# ---- bo'limlar (submenyular) ----
@admin.callback_query(F.data == "ad:st")
async def ad_st(call: CallbackQuery):
    await show(call, "📊 <b>Statistika</b>", kb([
        [("🟢 LIVE statistika", "ad:stats")],
        [("📈 Kunlik grafik", "ad:chart:7"), ("🔥 Top userlar", "ad:top")],
        [("🎵 Musiqa statistika", "ad:mstat"), ("📥 Download log", "ad:log:all")],
        BACK,
    ]))


@admin.callback_query(F.data == "ad:us")
async def ad_us(call: CallbackQuery):
    await show(call, "👥 <b>Userlar</b>", kb([
        [("🔍 User qidirish", "ad:find"), ("👥 Userlar ro'yxati", "ad:users")],
        [("🔥 Top userlar", "ad:top"), ("💎 PRO userlar", "ad:pro")],
        BACK,
    ]))


@admin.callback_query(F.data == "ad:ms")
async def ad_ms(call: CallbackQuery):
    await show(call, "📢 <b>Xabarlar</b>", kb([
        [("📢 Broadcast (hozir yoki keyinroq)", "ad:bc")],
        [("🕒 Rejalashtirilganlar", "ad:sch")],
        BACK,
    ]))


@admin.callback_query(F.data == "ad:pm")
async def ad_pm(call: CallbackQuery):
    async with pool.acquire() as c:
        ncards = await c.fetchval("SELECT count(*) FROM pay_cards WHERE active")
        npend = await c.fetchval("SELECT count(*) FROM pay_requests WHERE status='pending'")
    plans = await get_plans()
    auto = "✅ ulangan" if PAY_TOKEN else "❌ ulanmagan (PAY_PROVIDER_TOKEN)"
    await show(call,
               f"💎 <b>PRO / Promo / To'lov</b>\n\n"
               f"Click/Payme: {auto}\n"
               f"Kartalar: {ncards} ta faol\n"
               f"Tariflar: {len(plans)} ta\n"
               f"Tekshirilmagan cheklar: {npend} ta",
               kb([
                   [("💎 PRO userlar", "ad:pro"), ("🎟 Promo-kodlar", "ad:promo")],
                   [("💳 Kartalar", "ad:cards"), ("📦 Tariflar", "ad:plans")],
                   [(f"🧾 Cheklar ({npend})", "ad:reqs"), ("💰 To'lovlar", "ad:rev")],
                   BACK,
               ]))


@admin.callback_query(F.data == "ad:sys")
async def ad_sys(call: CallbackQuery):
    await show(call, "🩺 <b>Tizim</b>", kb([
        [("🩺 Diagnostika", "ad:diag"), ("🧪 YouTube testi", "ad:ytest")],
        [("🍪 Cookies", "ad:cookies"), ("🔄 yt-dlp yangilash", "ad:upd")],
        [("💾 Zaxira (backup)", "ad:backup"), ("📥 Download log", "ad:log:all")],
        [("♻️ Qayta ishga tushirish", "ad:restart")],
        BACK,
    ]))


# ---- Statistika ----
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
                   count(*) FILTER (WHERE joined > now() - interval '7 days') AS d7,
                   count(*) FILTER (WHERE referrer IS NOT NULL) AS refs
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
        pay = await c.fetchrow("SELECT count(*) AS n, COALESCE(sum(amount),0) AS s FROM payments")
    names = {"video": "🎬 Video", "music": "🎵 Musiqa", "round": "⭕ Dumaloq", "voice": "🎤 Ovoz orqali"}
    dl = "\n".join(f"{names.get(r['kind'], r['kind'])}: {r['total']} (bugun {r['d1']})" for r in d) or "—"
    now = datetime.now(LOCAL)
    text = (
        f"📊 <b>LIVE statistika</b> <i>{now:%H:%M:%S}</i>\n\n"
        f"🟢 Hozir online (5 daqiqa): <b>{u['online']}</b>\n"
        f"👥 Jami userlar: <b>{u['total']}</b>  (faol {u['act']})\n"
        f"🆕 Bugun: {u['d1']}   Hafta: {u['d7']}\n"
        f"💎 PRO: {u['pro']}   🚫 Ban: {u['ban']}   🎁 Referal: {u['refs']}\n"
        f"📢 Majburiy kanallar: {nch}\n"
        f"💰 To'lovlar: {pay['n']} ta ({pay['s'] / 100:,.0f})\n\n"
        f"📥 Bugungi yuklamalar: <b>{t['today']}</b>\n"
        f"📦 Jami yuklamalar: <b>{t['total']}</b>\n"
        f"⚠️ Bugungi xatolar: {t['errs']}\n\n"
        f"<b>Turlar bo'yicha:</b>\n{dl}"
    )
    await show(call, text, kb([[("🔄 Yangilash", "ad:stats")], BACK]))


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


@admin.callback_query(F.data == "ad:top")
async def ad_top(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("""
            SELECT u.id, u.name, u.username, count(*) AS n
            FROM downloads d JOIN users u ON u.id = d.user_id
            WHERE d.ok AND d.kind IN ('video','music','round','voice')
            GROUP BY u.id, u.name, u.username ORDER BY n DESC LIMIT 10""")
    text = "🔥 <b>Top foydalanuvchilar</b>\n\n"
    text += "\n".join(
        f"{i}. {ulabel(r['name'], r['username'])} — <b>{r['n']}</b> (<code>{r['id']}</code>)"
        for i, r in enumerate(rows, 1)) or "Hali ma'lumot yo'q."
    btns = [[(f"{i}. {ulabel_btn(r['name'], r['username'], 20)}", f"ad:u:{r['id']}")]
            for i, r in enumerate(rows[:5], 1)]
    await show(call, text, kb(btns + [BACK]))


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


@admin.callback_query(F.data.startswith("ad:log:"))
async def ad_log(call: CallbackQuery):
    only_err = call.data.split(":")[2] == "err"
    sql = ("SELECT d.*, u.username FROM downloads d LEFT JOIN users u ON u.id = d.user_id "
           "WHERE d.kind <> 'search'" + (" AND NOT d.ok" if only_err else "")
           + " ORDER BY d.ts DESC LIMIT 12")
    async with pool.acquire() as c:
        rows = await c.fetch(sql)
    lines = []
    for r in rows:
        who = f"@{e(r['username'])}" if r["username"] else f"<code>{r['user_id']}</code>"
        line = f"{lt(r['ts'])} {'✅' if r['ok'] else '❌'} {r['kind']}/{r['platform'] or '-'} {who}"
        if r["err"]:
            line += f"\n   <code>{e(r['err'][:140])}</code>"
        lines.append(line)
    text = f"📥 <b>Download log</b> ({'faqat xatolar' if only_err else 'hammasi'})\n\n"
    text += "\n".join(lines) or "Bo'sh."
    await show(call, text, kb([[("Hammasi", "ad:log:all"), ("Faqat xatolar", "ad:log:err")],
                               [("🔄 Yangilash", f"ad:log:{'err' if only_err else 'all'}")], BACK]))


# ---- Foydalanuvchi kartasi / qidirish / ban / PRO / DM ----
async def user_card(uid: int):
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT * FROM users WHERE id=$1", uid)
        cnt = await c.fetch(
            "SELECT kind, count(*) AS n FROM downloads WHERE user_id=$1 AND ok "
            "AND kind IN ('video','music','round','voice') GROUP BY kind", uid)
        refs = await c.fetchval("SELECT count(*) FROM users WHERE referrer=$1", uid)
    if not r:
        return None
    k = {x["kind"]: x["n"] for x in cnt}
    now = datetime.now(timezone.utc)
    pro = r["pro_until"] and r["pro_until"] > now
    text = (
        f"👤 <b>{e(r['name'])}</b>\n"
        f"ID: <code>{r['id']}</code>\n"
        f"Username: {'@' + e(r['username']) if r['username'] else '—'}\n"
        f"Til: {e(r['lang'] or 'uz')}   Taklif qilgan: {refs}\n"
        f"Qo'shilgan: {lt(r['joined'], '%Y-%m-%d %H:%M')}\n"
        f"Oxirgi faollik: {lt(r['last_seen'], '%Y-%m-%d %H:%M')}\n"
        f"🎬 Video: {k.get('video', 0)}   🎵 Musiqa: {k.get('music', 0)}   "
        f"⭕ Dumaloq: {k.get('round', 0)}   🎤 Ovoz: {k.get('voice', 0)}\n"
        f"Holat: {'🚫 bloklangan' if r['banned'] else '✅ faol'}\n"
        f"💎 PRO: {('✅ ' + lt(r['pro_until'], '%Y-%m-%d') + ' gacha') if pro else '—'}"
    )
    ban = ("✅ Unban", f"ad:unban:{uid}") if r["banned"] else ("🚫 Ban", f"ad:ban:{uid}")
    return text, kb([
        [ban, ("✉️ Xabar yuborish", f"ad:dm:{uid}")],
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
            rows = await c.fetch("SELECT id, name, username FROM users WHERE id=$1", int(q))
        else:
            rows = await c.fetch(
                "SELECT id, name, username FROM users WHERE lower(username)=lower($1) OR name ILIKE $2 "
                "OR username ILIKE $2 LIMIT 8", q, f"%{q}%")
    if not rows:
        return await m.answer("😕 Topilmadi. Boshqa so'z yuboring yoki /cancel.")
    await state.clear()
    if len(rows) == 1:
        text, markup = await user_card(rows[0]["id"])
        return await m.answer(text, parse_mode="HTML", reply_markup=markup)
    await m.answer("Bir nechta topildi:", reply_markup=kb(
        [[(f"{ulabel_btn(r['name'], r['username'], 24)} ({r['id']})", f"ad:u:{r['id']}")] for r in rows] + [BACK]))


@admin.callback_query(F.data.startswith("ad:ban:") | F.data.startswith("ad:unban:"))
async def ad_ban(call: CallbackQuery):
    _, act, uid = call.data.split(":")
    uid = int(uid)
    if is_admin(uid):
        return await call.answer("Adminni bloklab bo'lmaydi.", show_alert=True)
    async with pool.acquire() as c:
        await c.execute("UPDATE users SET banned=$2 WHERE id=$1", uid, act == "ban")
    await show(call, *(await user_card(uid)))


@admin.callback_query(F.data.startswith("ad:dm:"))
async def ad_dm(call: CallbackQuery, state: FSMContext):
    uid = int(call.data.split(":")[2])
    await state.set_state(S.dm)
    await state.update_data(target=uid)
    await call.message.answer(f"✉️ <code>{uid}</code> ga yuboriladigan xabarni yuboring "
                              f"(matn, rasm, video — istalgani). /cancel — bekor.", parse_mode="HTML")
    await call.answer()


@admin.message(S.dm)
async def dm_send(m: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    try:
        await bot.copy_message(data["target"], m.chat.id, m.message_id)
        await m.answer("✅ Yuborildi.", reply_markup=menu())
    except Exception as ex:
        await m.answer(f"❌ Yuborib bo'lmadi: <code>{e(short_err(ex))}</code>", parse_mode="HTML")


@admin.callback_query(F.data.startswith("ad:pro:") & (F.data != "ad:pro"))
async def ad_pro_set(call: CallbackQuery):
    _, _, uid, days = call.data.split(":")
    uid, days = int(uid), int(days)
    if days == 0:
        async with pool.acquire() as c:
            await c.execute("UPDATE users SET pro_until=NULL WHERE id=$1", uid)
    else:
        await grant_pro(uid, days)
        try:
            await bot.send_message(uid, tr(await get_lang(uid), "pro_given", days=days))
        except Exception:
            pass
    await show(call, *(await user_card(uid)))


@admin.callback_query(F.data == "ad:pro")
async def ad_pro(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id, name, username, pro_until FROM users WHERE pro_until > now() "
            "ORDER BY pro_until LIMIT 15")
    text = (f"💎 <b>PRO foydalanuvchilar</b>\n\nFaol PRO: {len(rows)}\n\n"
            "Yangi PRO berish: «🔍 User topib PRO berish» → userni toping → 💎 tugmalari.")
    btns = [[(f"💎 {ulabel_btn(r['name'], r['username'], 18)} — {lt(r['pro_until'], '%d.%m')}", f"ad:u:{r['id']}")]
            for r in rows]
    await show(call, text, kb(btns + [[("🔍 User topib PRO berish", "ad:find")], BACK]))


# ---- Userlar ro'yxati ----
@admin.callback_query(F.data == "ad:users")
async def ad_users(call: CallbackQuery, state: FSMContext):
    await state.clear()
    async with pool.acquire() as c:
        total = await c.fetchval("SELECT count(*) FROM users")
        last = await c.fetch("SELECT id, name, username FROM users ORDER BY joined DESC LIMIT 10")
    text = f"👥 <b>Foydalanuvchilar: {total}</b>\n\nOxirgi 10 ta:\n"
    text += "\n".join(f"• {ulabel(r['name'], r['username'])} — <code>{r['id']}</code>" for r in last)
    btns = [[(ulabel_btn(r["name"], r["username"], 28), f"ad:u:{r['id']}")] for r in last[:5]]
    await show(call, text, kb(btns + [
        [("🔍 Qidirish", "ad:find")],
        [("📄 Ro'yxatni yuklab olish", "ad:export")],
        BACK,
    ]))


@admin.callback_query(F.data == "ad:export")
async def ad_export(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM users ORDER BY joined")
    lines = [f"{r['id']} | {r['name']} | @{r['username'] or '-'} | {lt(r['joined'], '%Y-%m-%d')} | "
             f"{'banned' if r['banned'] else 'ok'}" for r in rows]
    await call.message.answer_document(BufferedInputFile("\n".join(lines).encode(), "users.txt"),
                                       caption=f"Jami: {len(rows)}")
    await call.answer()


# ---- Majburiy obuna ----
async def channels_view(call: CallbackQuery):
    async with pool.acquire() as c:
        chans = await c.fetch("SELECT chat_id, title FROM channels ORDER BY title")
    on = CFG["sub_enabled"] == "1"
    state_txt = "✅ yoqilgan" if on else "❌ o'chirilgan"
    text = f"🔗 <b>Majburiy obuna</b>\nTekshiruv: {state_txt}\n\n"
    text += "\n".join(f"• {e(ch['title'])}" for ch in chans) or "Hali kanal qo'shilmagan."
    text += "\n\n💡 Admin hisobingiz tekshiruvdan o'tkazib yuboriladi. Sinash uchun boshqa akkauntdan kiring."
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
    await m.answer(f"✅ Qo'shildi: <b>{e(chat.title)}</b>", parse_mode="HTML", reply_markup=menu())


# ---- Broadcast (hozir yoki rejalashtirilgan) ----
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
    await m.answer(f"Bu xabar <b>{n}</b> ta foydalanuvchiga yuboriladi. Qachon yuboramiz?",
                   parse_mode="HTML",
                   reply_markup=kb([
                       [("✅ Hozir yuborish", "ad:bcgo:0")],
                       [("🕒 30 daqiqadan keyin", "ad:bcgo:30"), ("🕒 1 soatdan keyin", "ad:bcgo:60")],
                       [("🕒 3 soatdan keyin", "ad:bcgo:180"), ("🕒 Ertaga (24 soat)", "ad:bcgo:1440")],
                       [("❌ Bekor", "ad:home")],
                   ]))


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
    try:
        await bot.send_message(admin_id, f"✅ Yuborildi: {ok}\n❌ Yetib bormadi: {fail}")
    except Exception:
        pass


def spawn(coro):
    t = asyncio.create_task(coro)
    BG.add(t)
    t.add_done_callback(BG.discard)
    return t


@admin.callback_query(F.data.startswith("ad:bcgo:"))
async def bc_go(call: CallbackQuery, state: FSMContext):
    minutes = int(call.data.split(":")[2])
    data = await state.get_data()
    await state.clear()
    if "mid" not in data:
        return await call.answer("Xabar topilmadi.", show_alert=True)
    if minutes <= 0:
        spawn(do_broadcast(call.from_user.id, data["cid"], data["mid"]))
        return await show(call, "⏳ Yuborish boshlandi. Tugagach xabar beraman.", kb([BACK]))
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO scheduled(run_at, admin_id, chat_id, msg_id) "
            "VALUES(now() + make_interval(mins => $1), $2, $3, $4)",
            minutes, call.from_user.id, data["cid"], data["mid"])
    await show(call, f"🕒 Rejalashtirildi: {minutes} daqiqadan keyin yuboriladi.", kb([BACK]))


@admin.callback_query(F.data == "ad:sch")
async def ad_sch(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT id, run_at FROM scheduled WHERE NOT done ORDER BY run_at LIMIT 15")
    text = "🕒 <b>Rejalashtirilgan xabarlar</b>\n\n"
    text += "\n".join(f"#{r['id']} — {lt(r['run_at'])}" for r in rows) or "Hozircha yo'q."
    btns = [[(f"🗑 #{r['id']} ({lt(r['run_at'])})", f"ad:schdel:{r['id']}")] for r in rows]
    await show(call, text, kb(btns + [BACK]))


@admin.callback_query(F.data.startswith("ad:schdel:"))
async def ad_schdel(call: CallbackQuery):
    sid = int(call.data.split(":")[2])
    async with pool.acquire() as c:
        await c.execute("UPDATE scheduled SET done=TRUE WHERE id=$1", sid)
    await ad_sch(call)


# ---- Promo-kodlar / to'lovlar ----
@admin.callback_query(F.data == "ad:promo")
async def ad_promo(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT code, days, max_uses, used FROM promo ORDER BY created DESC LIMIT 15")
    text = "🎟 <b>Promo-kodlar</b>\n\n"
    text += "\n".join(
        f"<code>{e(r['code'])}</code> — {r['days']} kun, ishlatilgan {r['used']}/{r['max_uses'] or '∞'}"
        for r in rows) or "Hali yo'q."
    btns = [[(f"🗑 {r['code']}", f"ad:promodel:{r['code']}")] for r in rows]
    await show(call, text, kb(btns + [[("➕ Yangi promo-kod", "ad:promoadd")], BACK]))


@admin.callback_query(F.data.startswith("ad:promodel:"))
async def ad_promodel(call: CallbackQuery):
    code = call.data.split(":", 2)[2]
    async with pool.acquire() as c:
        await c.execute("DELETE FROM promo WHERE code=$1", code)
    await ad_promo(call)


@admin.callback_query(F.data == "ad:promoadd")
async def ad_promoadd(call: CallbackQuery, state: FSMContext):
    await state.set_state(S.promo)
    await call.message.answer(
        "🎟 Yangi promo-kod: <code>KUN SONI [KOD]</code>\n\n"
        "Masalan: <code>30 100</code> — 30 kun PRO, 100 kishi ishlata oladi (kod avtomatik).\n"
        "<code>7 0 YANGIYIL</code> — 7 kun, cheksiz, kod YANGIYIL.\n/cancel — bekor.",
        parse_mode="HTML")
    await call.answer()


@admin.message(S.promo, F.text)
async def promo_create(m: Message, state: FSMContext):
    parts = m.text.split()
    if len(parts) < 1 or not parts[0].isdigit() or (len(parts) > 1 and not parts[1].isdigit()):
        return await m.answer("❌ Format: KUN SONI [KOD]  (masalan: 30 100)")
    days = int(parts[0])
    max_uses = int(parts[1]) if len(parts) > 1 else 0
    code = (parts[2].upper() if len(parts) > 2 else uuid.uuid4().hex[:8].upper())
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        return await m.answer("❌ Kod faqat lotin harf, raqam, _ va - dan iborat bo'lsin (3–32 belgi).")
    async with pool.acquire() as c:
        ok = await c.fetchval(
            "INSERT INTO promo(code, days, max_uses) VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING 1",
            code, days, max_uses)
    if not ok:
        return await m.answer("❌ Bu kod allaqachon mavjud. Boshqasini yozing.")
    await state.clear()
    await m.answer(f"✅ Promo-kod yaratildi: <code>{code}</code>\n{days} kun PRO, "
                   f"{max_uses or '∞'} marta.\nFoydalanuvchi: <code>/promo {code}</code>",
                   parse_mode="HTML", reply_markup=menu())


@admin.callback_query(F.data == "ad:rev")
async def ad_rev(call: CallbackQuery):
    async with pool.acquire() as c:
        tot = await c.fetchrow("SELECT count(*) AS n, COALESCE(sum(amount),0) AS s FROM payments")
        rows = await c.fetch(
            "SELECT p.*, u.name, u.username FROM payments p LEFT JOIN users u ON u.id = p.user_id "
            "ORDER BY p.ts DESC LIMIT 10")
    text = f"💰 <b>To'lovlar</b>\nJami: {tot['n']} ta, {tot['s'] / 100:,.0f}\n\n"
    text += "\n".join(
        f"{lt(r['ts'])} — {ulabel(r['name'], r['username'])}: {r['days']} kun, "
        f"{r['amount'] / 100:,.0f} {e(r['currency'])}" for r in rows) or "Hali to'lov yo'q."
    await show(call, text, kb([BACK]))


# ---- Kartalar ----
@admin.callback_query(F.data == "ad:cards")
async def ad_cards(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT id, title, number, holder, active FROM pay_cards ORDER BY id")
    text = "💳 <b>To'lov kartalari</b>\n\n"
    text += "\n".join(
        f"{'🟢' if r['active'] else '⚪️'} #{r['id']} <code>{e(fmt_card(r['number']))}</code> — "
        f"{e(r['holder'])}" + (f" ({e(r['title'])})" if r['title'] else "")
        for r in rows) or "Hali karta yo'q."
    text += "\n\n🟢 — foydalanuvchilarga ko'rinadi, ⚪️ — yashirin."
    btns = []
    for r in rows:
        btns.append([(("⏸ O'chirib qo'yish" if r["active"] else "▶️ Yoqish") + f" #{r['id']}", f"ad:cardtg:{r['id']}"),
                     (f"🗑 #{r['id']}", f"ad:carddel:{r['id']}")])
    await show(call, text, kb(btns + [[("➕ Karta qo'shish", "ad:cardadd")], [("⬅️ Orqaga", "ad:pm")]]))


@admin.callback_query(F.data.startswith("ad:cardtg:"))
async def ad_cardtg(call: CallbackQuery):
    async with pool.acquire() as c:
        await c.execute("UPDATE pay_cards SET active = NOT active WHERE id=$1", int(call.data.split(":")[2]))
    await ad_cards(call)


@admin.callback_query(F.data.startswith("ad:carddel:"))
async def ad_carddel(call: CallbackQuery):
    async with pool.acquire() as c:
        await c.execute("DELETE FROM pay_cards WHERE id=$1", int(call.data.split(":")[2]))
    await ad_cards(call)


@admin.callback_query(F.data == "ad:cardadd")
async def ad_cardadd(call: CallbackQuery, state: FSMContext):
    await state.set_state(S.card)
    await call.message.answer(
        "💳 Yangi karta: <code>RAQAM | Egasining ismi | Bank (ixtiyoriy)</code>\n\n"
        "Masalan:\n<code>8600 1234 5678 9012 | Ali Valiyev | Uzcard</code>\n\n/cancel — bekor.",
        parse_mode="HTML")
    await call.answer()


@admin.message(S.card, F.text)
async def card_create(m: Message, state: FSMContext):
    parts = [p.strip() for p in m.text.split("|")]
    num = re.sub(r"\D", "", parts[0])
    if len(num) < 12 or len(num) > 19 or len(parts) < 2 or not parts[1]:
        return await m.answer("❌ Format: RAQAM | Ism | Bank\nMasalan: 8600 1234 5678 9012 | Ali Valiyev | Uzcard")
    async with pool.acquire() as c:
        await c.execute("INSERT INTO pay_cards(title, number, holder) VALUES($1,$2,$3)",
                        parts[2] if len(parts) > 2 else "", num, parts[1])
    await state.clear()
    await m.answer(f"✅ Karta qo'shildi: <code>{fmt_card(num)}</code>", parse_mode="HTML", reply_markup=menu())


# ---- Tariflar ----
@admin.callback_query(F.data == "ad:plans")
async def ad_plans(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT days, price FROM pay_plans ORDER BY days")
    if rows:
        text = "📦 <b>PRO tariflari</b>\n\n" + "\n".join(f"• {r['days']} kun — {money(r['price'])} so'm" for r in rows)
    else:
        env = "\n".join(f"• {d} kun — {money(p)} so'm" for d, p in sorted(PRO_PLANS.items()))
        text = ("📦 <b>PRO tariflari</b>\n\nBazada tarif yo'q." +
                (f" Hozir PRO_PLANS dan olinmoqda:\n{env}" if env else " Tarif qo'shing."))
    btns = [[(f"🗑 {r['days']} kun", f"ad:plandel:{r['days']}")] for r in rows]
    await show(call, text, kb(btns + [[("➕ Tarif qo'shish", "ad:planadd")], [("⬅️ Orqaga", "ad:pm")]]))


@admin.callback_query(F.data.startswith("ad:plandel:"))
async def ad_plandel(call: CallbackQuery):
    async with pool.acquire() as c:
        await c.execute("DELETE FROM pay_plans WHERE days=$1", int(call.data.split(":")[2]))
    await ad_plans(call)


@admin.callback_query(F.data == "ad:planadd")
async def ad_planadd(call: CallbackQuery, state: FSMContext):
    await state.set_state(S.plan)
    await call.message.answer(
        "📦 Yangi tarif: <code>KUN NARX</code> (so'mda)\n\nMasalan: <code>30 15000</code>\n"
        "Bir xil kun qayta yozilsa, narxi yangilanadi.\n/cancel — bekor.", parse_mode="HTML")
    await call.answer()


@admin.message(S.plan, F.text)
async def plan_create(m: Message, state: FSMContext):
    parts = m.text.replace(",", "").split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts) or int(parts[0]) < 1 or int(parts[1]) < 1:
        return await m.answer("❌ Format: KUN NARX  (masalan: 30 15000)")
    async with pool.acquire() as c:
        await c.execute("INSERT INTO pay_plans(days, price) VALUES($1,$2) "
                        "ON CONFLICT(days) DO UPDATE SET price=$2", int(parts[0]), int(parts[1]))
    await state.clear()
    await m.answer(f"✅ Tarif saqlandi: {parts[0]} kun — {money(parts[1])} so'm", reply_markup=menu())


# ---- Cheklar ----
@admin.callback_query(F.data == "ad:reqs")
async def ad_reqs(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT r.id, r.days, r.price, r.ts, u.name, u.username FROM pay_requests r "
            "LEFT JOIN users u ON u.id = r.user_id WHERE r.status='pending' ORDER BY r.id DESC LIMIT 15")
    text = "🧾 <b>Tekshirilmagan cheklar</b>\n\n"
    text += "\n".join(f"#{r['id']} {lt(r['ts'])} — {ulabel(r['name'], r['username'])}: "
                      f"{r['days']} kun, {money(r['price'])} so'm" for r in rows) or "Yo'q ✅"
    btns = [[(f"👁 #{r['id']} — {ulabel_btn(r['name'], r['username'], 16)}", f"pr:view:{r['id']}")] for r in rows]
    await show(call, text, kb(btns + [[("⬅️ Orqaga", "ad:pm")]]))


@admin.callback_query(F.data.startswith("pr:view:"))
async def pr_view(call: CallbackQuery):
    rid = int(call.data.split(":")[2])
    async with pool.acquire() as c:
        r = await c.fetchrow(
            "SELECT r.*, u.name, u.username FROM pay_requests r LEFT JOIN users u ON u.id=r.user_id "
            "WHERE r.id=$1", rid)
    if not r:
        return await call.answer("Topilmadi.", show_alert=True)
    caption = (f"🧾 Chek #{rid} ({r['status']})\n👤 {r['name'] or '?'}" +
               (f" @{r['username']}" if r["username"] else "") +
               f" ({r['user_id']})\n💎 {r['days']} kun — {money(r['price'])} so'm")
    markup = kb([[("✅ Tasdiqlash", f"pr:ok:{rid}"), ("❌ Rad etish", f"pr:no:{rid}")]]) \
        if r["status"] == "pending" else None
    if r["is_photo"]:
        await bot.send_photo(call.message.chat.id, r["file_id"], caption=caption, reply_markup=markup)
    else:
        await bot.send_document(call.message.chat.id, r["file_id"], caption=caption, reply_markup=markup)
    await call.answer()


@admin.callback_query(F.data.startswith("pr:ok:") | F.data.startswith("pr:no:"))
async def pr_decide(call: CallbackQuery):
    _, act, rid = call.data.split(":")
    rid = int(rid)
    ok = act == "ok"
    async with pool.acquire() as c:
        async with c.transaction():
            r = await c.fetchrow(
                "UPDATE pay_requests SET status=$2, admin_id=$3, decided=now() "
                "WHERE id=$1 AND status='pending' RETURNING user_id, days, price",
                rid, "ok" if ok else "no", call.from_user.id)
            if r and ok:
                await c.execute(
                    "INSERT INTO payments(user_id, days, amount, currency, charge_id) "
                    "VALUES($1,$2,$3,'UZS',$4)", r["user_id"], r["days"], r["price"] * 100, f"manual:{rid}")
                await c.execute(
                    "UPDATE users SET pro_until = GREATEST(now(), COALESCE(pro_until, now())) "
                    "+ make_interval(days => $2) WHERE id=$1", r["user_id"], r["days"])
    if not r:
        return await call.answer("Bu chek allaqachon ko'rib chiqilgan.", show_alert=True)
    try:
        ulang = await get_lang(r["user_id"])
        await bot.send_message(r["user_id"], tr(ulang, "chek_ok" if ok else "chek_no", days=r["days"]))
    except Exception:
        logging.warning("chek natijasi userga yuborilmadi: %s", r["user_id"])
    try:
        mark = "✅ Tasdiqlandi, PRO yoqildi" if ok else "❌ Rad etildi"
        await call.message.edit_caption(
            caption=(call.message.caption or "") + f"\n\n{mark} — {call.from_user.full_name}",
            reply_markup=None)
    except Exception:
        pass
    await call.answer("✅ PRO yoqildi" if ok else "Rad etildi")


# ---- Sozlamalar ----
async def settings_view(call: CallbackQuery):
    rows = [[(f"{'✅' if CFG[k] == '1' else '❌'} {label}", f"ad:tg:{k}:set")]
            for k, label in TOGGLES.items()]
    rows += [[(f"{label}: {CFG[k]}", f"ad:tx:{k}")] for k, label in NUMS.items()]
    rows += [[(label, f"ad:tx:{k}")] for k, label in TEXTS.items()]
    rows.append(BACK)
    await show(call, "⚙️ <b>Sozlamalar</b>\nTugmani bosib yoqing/o'chiring. Limit — har bir user uchun kuniga "
                     "nechta yuklash (0 = cheksiz; PRO va adminlar uchun doim cheksiz).", kb(rows))


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
    elif back == "home":
        await show(call, PANEL_TITLE, menu())
    else:
        await settings_view(call)


@admin.callback_query(F.data.startswith("ad:tx:"))
async def ad_tx(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[2]
    await state.set_state(S.edit_text)
    await state.update_data(key=key)
    if key in NUMS:
        txt = f"{NUMS[key]}: <b>{CFG[key]}</b>\n\nYangi sonni yuboring (0 = o'chirish/cheksiz). /cancel — bekor."
    else:
        hint = "\n\n<code>{bot}</code> — bot username'i o'rniga qo'yiladi." if key == "caption" else ""
        txt = f"Hozirgi matn:\n\n{e(CFG[key])}\n\nYangi matnni yuboring. /cancel — bekor.{hint}"
    await call.message.answer(txt, parse_mode="HTML")
    await call.answer()


@admin.message(S.edit_text, F.text)
async def tx_save(m: Message, state: FSMContext):
    data = await state.get_data()
    key = data["key"]
    if key in NUMS:
        if not m.text.strip().isdigit():
            return await m.answer("❌ Faqat son yuboring (masalan 20) yoki /cancel.")
        await set_cfg(key, m.text.strip())
    else:
        await set_cfg(key, m.html_text)
    await state.clear()
    await m.answer("✅ Saqlandi.", reply_markup=menu())


# ---- Tizim: diagnostika, YouTube testi, cookies, yangilash, backup, restart ----
def cookie_status() -> str:
    if not os.path.exists(COOKIE_LOCAL):
        return "yo'q"
    try:
        txt = open(COOKIE_LOCAL, encoding="utf-8", errors="ignore").read()
        age = (time.time() - os.path.getmtime(COOKIE_LOCAL)) / 3600
        login = "LOGIN_INFO" in txt or "SAPISID" in txt or "__Secure-3PSID" in txt
        n = sum(1 for ln in txt.splitlines() if ln.strip() and not ln.startswith("#"))
        return f"bor ({n} qator, login belgisi: {'✅' if login else '❌'}, yangilangan {age:.0f} soat oldin)"
    except Exception:
        return "bor (o'qib bo'lmadi)"


def ver(pkg: str) -> str:
    try:
        return md.version(pkg)
    except Exception:
        return "yo'q"


def diag_info() -> str:
    ff = shutil.which("ffmpeg")
    ffv = ""
    if ff:
        try:
            ffv = subprocess.run([ff, "-version"], capture_output=True, text=True,
                                 timeout=5).stdout.splitlines()[0][:48]
        except Exception:
            ffv = "?"
    try:
        running = yt_dlp.version.__version__
    except Exception:
        running = "?"
    installed = ver("yt-dlp")
    note = "" if running == installed else "  ⚠️ qayta ishga tushirish kerak"
    rt = {n: shutil.which(n) for n in ("deno", "node", "bun")}
    rt_s = ", ".join(f"{n}: {'✅' if p else '❌'}" for n, p in rt.items())
    lines = [
        "🩺 <b>Diagnostika</b>\n",
        f"Python: {sys.version.split()[0]}",
        f"yt-dlp (ishlayotgan): <b>{e(running)}</b>",
        f"yt-dlp (o'rnatilgan): {e(installed)}{note}",
        f"yt-dlp-ejs: {e(ver('yt-dlp-ejs'))}",
        f"JS runtime: {rt_s}",
        f"ffmpeg: {'✅ ' + e(ffv) if ff else '❌ yo`q'}",
        f"shazamio: {e(ver('shazamio'))}   AudD: {'✅' if AUDD_TOKEN else '—'}",
        f"aiogram: {e(ver('aiogram'))}",
        f"Cookies: {e(cookie_status())}",
        f"Proxy: {'✅' if PROXY else '—'}",
        f"To'lov: {'✅' if (PAY_TOKEN and PRO_PLANS) else '—'}",
        f"YouTube bloki: {'⛔ faol' if time.time() < _YT_BLOCK['until'] else 'yo`q'}",
        f"Oxirgi ishlagan klient: {_YT_BEST['i']}",
        f"Limitlar: bir vaqtda {MAX_DL} yuklash, vaqt chegarasi {DL_BUDGET}s",
    ]
    return "\n".join(lines)


def yt_test() -> str:
    out = ["🧪 <b>YouTube testi</b>\n"]
    for name, args in (("standart", None), ("tv+safari", {"player_client": ["tv", "web_safari"]}),
                       ("ios", {"player_client": ["ios"]}),
                       ("android_vr", {"player_client": ["android_vr"]}),
                       ("mweb", {"player_client": ["mweb"]})):
        t = time.monotonic()
        try:
            opts = base_opts() | {"skip_download": True, "format": "bestaudio/best"}
            if args:
                opts["extractor_args"] = {"youtube": args}
            with yt_dlp.YoutubeDL(opts) as y:
                y.extract_info(TEST_URL, download=False)
            out.append(f"✅ {name} ({time.monotonic() - t:.1f}s)")
        except Exception as ex:
            out.append(f"❌ {name}: <code>{e(short_err(ex)[:110])}</code>")
    return "\n".join(out)


@admin.message(Command("diag"))
async def cmd_diag(m: Message):
    await m.answer(await run(diag_info), parse_mode="HTML")


@admin.callback_query(F.data == "ad:diag")
async def ad_diag(call: CallbackQuery):
    await show(call, await run(diag_info), kb([[("🔄 Yangilash", "ad:diag")], BACK]))


@admin.callback_query(F.data == "ad:ytest")
async def ad_ytest(call: CallbackQuery):
    await call.answer("Tekshirilmoqda, 1 daqiqagacha...")
    wait = await call.message.answer("🧪 YouTube tekshirilmoqda...")
    text = await run(yt_test)
    try:
        await wait.delete()
    except Exception:
        pass
    await call.message.answer(text, parse_mode="HTML")


def pip_update():
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-U", "--pre", "--disable-pip-version-check",
         "yt-dlp[default]"], capture_output=True, text=True, timeout=240)
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-3:])
    return r.returncode, tail


@admin.callback_query(F.data == "ad:upd")
async def ad_upd(call: CallbackQuery):
    await call.answer("Yangilanmoqda...")
    before = ver("yt-dlp")
    wait = await call.message.answer("🔄 yt-dlp yangilanmoqda (1–2 daqiqa)...")
    try:
        code, tail = await run(pip_update)
    except Exception as ex:
        code, tail = 1, short_err(ex)
    after = ver("yt-dlp")
    try:
        await wait.delete()
    except Exception:
        pass
    if code == 0:
        text = (f"✅ yt-dlp: {e(before)} → <b>{e(after)}</b>\n\n"
                f"Yangi versiya ishlashi uchun «♻️ Qayta ishga tushirish» ni bosing.")
    else:
        text = (f"❌ Yangilab bo'lmadi:\n<code>{e(tail)}</code>\n\n"
                f"Server pip ga ruxsat bermasa, <code>requirements.txt</code> da "
                f"<code>yt-dlp[default]</code> qoldirib qayta deploy qiling.")
    await call.message.answer(text, parse_mode="HTML",
                              reply_markup=kb([[("♻️ Qayta ishga tushirish", "ad:restart")]]))


@admin.callback_query(F.data == "ad:restart")
async def ad_restart(call: CallbackQuery):
    await call.answer("Qayta ishga tushmoqda...")
    await call.message.answer("♻️ Bot qayta ishga tushmoqda, 10–20 soniyada tayyor bo'ladi.")
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


@admin.callback_query(F.data == "ad:cookies")
async def ad_cookies(call: CallbackQuery, state: FSMContext):
    await state.set_state(S.cookies)
    await call.message.answer(
        f"🍪 <b>YouTube cookies</b>\nHozir: {e(cookie_status())}\n\n"
        "Brauzerda YouTube ga kirib (alohida/inkognito oynada), «Get cookies.txt LOCALLY» kabi "
        "kengaytma bilan <b>cookies.txt</b> (Netscape format) eksport qiling va shu yerga "
        "<b>fayl qilib</b> yuboring.\n\n/cancel — bekor.\n"
        "O'chirish uchun: /delcookies", parse_mode="HTML")
    await call.answer()


async def save_cookies(txt: str):
    await set_cfg("cookies_data", txt)
    init_cookies()
    _YT_BLOCK["until"] = 0.0


@admin.message(S.cookies, F.document)
async def cookies_file(m: Message, state: FSMContext):
    doc = m.document
    if doc.file_size and doc.file_size > 2 * 1024 * 1024:
        return await m.answer("❌ Fayl juda katta (2 MB dan oshmasin).")
    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    txt = buf.getvalue().decode("utf-8", "ignore")
    if "youtube.com" not in txt and "google.com" not in txt:
        return await m.answer("❌ Bu cookies.txt ga o'xshamaydi (youtube.com/google.com topilmadi). "
                              "Netscape formatdagi fayl yuboring.")
    await save_cookies(txt)
    await state.clear()
    await m.answer(f"✅ Cookies saqlandi.\n{e(cookie_status())}\n\n"
                   f"«🧪 YouTube testi» bilan tekshirib ko'ring.", parse_mode="HTML",
                   reply_markup=menu())


@admin.message(Command("delcookies"))
async def del_cookies(m: Message, state: FSMContext):
    await state.clear()
    await set_cfg("cookies_data", "")
    try:
        os.remove(COOKIE_LOCAL)
    except OSError:
        pass
    init_cookies()
    await m.answer("🗑 Cookies o'chirildi.")


async def make_backup() -> bytes:
    data = {}
    async with pool.acquire() as c:
        for tbl in ("users", "channels", "admins", "favorites", "promo", "promo_used", "payments",
                    "pay_cards", "pay_plans", "pay_requests"):
            data[tbl] = [dict(r) for r in await c.fetch(f"SELECT * FROM {tbl}")]
        data["settings"] = [dict(r) for r in await c.fetch(
            "SELECT * FROM settings WHERE key <> 'cookies_data'")]
    return gzip.compress(json.dumps(data, default=str, ensure_ascii=False).encode("utf-8"))


async def send_backup(chat_ids):
    raw = await make_backup()
    name = f"backup_{datetime.now(LOCAL):%Y-%m-%d}.json.gz"
    for cid in chat_ids:
        try:
            await bot.send_document(cid, BufferedInputFile(raw, name),
                                    caption=f"💾 Zaxira nusxa ({len(raw) // 1024} KB)")
        except Exception:
            logging.exception("backup send")


@admin.callback_query(F.data == "ad:backup")
async def ad_backup(call: CallbackQuery):
    await call.answer("Tayyorlanmoqda...")
    await send_backup([call.from_user.id])


# ---- Adminlar ----
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
    await m.answer(f"✅ Admin qo'shildi: <code>{uid}</code>\nU botga /start yozsa, «{ADMIN_BTN}» tugmasi chiqadi.",
                   parse_mode="HTML", reply_markup=menu())


@admin.callback_query(F.data.startswith("ad:admdel:"))
async def ad_admdel(call: CallbackQuery):
    if call.from_user.id not in OWNERS:
        return await call.answer("Faqat asosiy admin.", show_alert=True)
    uid = int(call.data.split(":")[2])
    async with pool.acquire() as c:
        await c.execute("DELETE FROM admins WHERE id=$1", uid)
    ADMIN_SET.discard(uid)
    await admins_view(call)


# ====================== FON VAZIFALAR ======================
async def daily_report():
    async with pool.acquire() as c:
        u = await c.fetchrow(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE joined > now() - interval '1 day') AS new24, "
            "count(*) FILTER (WHERE last_seen > now() - interval '1 day') AS act24, "
            "count(*) FILTER (WHERE pro_until > now()) AS pro FROM users")
        d = await c.fetchrow(
            "SELECT count(*) FILTER (WHERE ok AND kind <> 'search') AS ok24, "
            "count(*) FILTER (WHERE NOT ok) AS err24 FROM downloads WHERE ts > now() - interval '1 day'")
        rev = await c.fetchval(
            "SELECT COALESCE(sum(amount),0) FROM payments WHERE ts > now() - interval '1 day'")
    text = (f"📬 <b>Kunlik hisobot</b>\n\n"
            f"👥 Jami: {u['total']}   🆕 24 soatda: {u['new24']}   🟢 Faol: {u['act24']}\n"
            f"💎 PRO: {u['pro']}\n"
            f"📥 Yuklamalar: {d['ok24']}   ⚠️ Xatolar: {d['err24']}\n"
            f"💰 To'lov (24 soat): {rev / 100:,.0f}")
    for aid in OWNERS:
        try:
            await bot.send_message(aid, text, parse_mode="HTML")
        except Exception:
            pass


async def scheduler_loop():
    while True:
        try:
            await asyncio.sleep(30)
            async with pool.acquire() as c:
                due = await c.fetch(
                    "UPDATE scheduled SET done=TRUE WHERE NOT done AND run_at <= now() "
                    "RETURNING admin_id, chat_id, msg_id")
            for r in due:
                spawn(do_broadcast(r["admin_id"], r["chat_id"], r["msg_id"]))
            now = datetime.now(LOCAL)
            today = now.strftime("%Y-%m-%d")
            if now.hour >= REPORT_HOUR and CFG.get("last_report") != today:
                await set_cfg("last_report", today)
                await daily_report()
                if now.weekday() == 0:  # dushanba kuni zaxira nusxa ham yuboriladi
                    await send_backup(list(OWNERS))
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("scheduler")


# ====================== ISHGA TUSHIRISH ======================
async def health(_):
    return web.Response(text="ok")


async def main_run():
    global BOT_USERNAME
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL kiritilmagan!")
    if not OWNERS:
        logging.warning("ADMIN_IDS bo'sh — admin panel hech kimga ochilmaydi!")
    if Shazam is None:
        logging.warning("shazamio o'rnatilmagan — ovoz orqali topish ishlamaydi!")
    await db_init()
    init_cookies()
    BOT_USERNAME = (await bot.get_me()).username

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8000"))).start()

    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Boshlash / Start"),
            BotCommand(command="fav", description="⭐ Sevimlilar"),
            BotCommand(command="top", description="🔥 Top 10"),
            BotCommand(command="ref", description="🎁 Do'st taklif qilish"),
            BotCommand(command="promo", description="🎟 Promo-kod"),
            BotCommand(command="buy", description="💎 PRO"),
            BotCommand(command="lang", description="🌐 Til / Язык"),
        ])
    except Exception:
        logging.exception("set_my_commands")

    spawn(scheduler_loop())
    dp.include_router(admin)
    dp.include_router(main)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main_run())
