import asyncio
import html
import io
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

import jdatetime
from aiohttp import ClientSession, ClientTimeout, web

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Integer,
    String,
    Text,
    create_engine,
    func,
    inspect,
    or_,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReactionTypeEmoji,
    ReplyKeyboardMarkup,
)

# ---------------------------------------------------------------------------
# تنظیمات اولیه
# هیچ اطلاعات حساسی داخل کد نیست؛ همه از متغیرهای محیطی (Environment) خوانده میشوند:
#   BOT_TOKEN, ADMIN_ID            -> الزامی
#   CARD_NUMBER, CARD_HOLDER       -> الزامی فقط برای اولین اجرا (بعدش از پنل ادمین قابل تغییر است)
#   DATABASE_URL                   -> آدرس دیتابیس Postgres (اگر نباشد SQLite محلی)
#   SUPPORT_INFO                   -> اختیاری
# ---------------------------------------------------------------------------


def _require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"Missing required environment variable: {name}. "
            "Set it in your hosting dashboard (Render -> Environment) and redeploy."
        )
    return value


TOKEN = _require_env("BOT_TOKEN")
try:
    ADMIN_ID = int(_require_env("ADMIN_ID"))
except ValueError:
    raise SystemExit("ADMIN_ID must be a numeric Telegram user id.")

database_url = os.environ.get("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

if database_url:
    engine = create_engine(database_url, pool_pre_ping=True)
else:
    engine = create_engine("sqlite:///bot_data.db")

Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)
dp = Dispatcher(storage=MemoryStorage())

esc = html.escape
ARROW = "⬅"  # جهت فلش: از قیمت اصلی به قیمت تخفیفخورده (متن راستبهچپ)

BTN_BUY = "🛒 خرید سرویس"
BTN_MY = "📦 سرویسهای من"
BTN_TARIFFS = "📈 تعرفه سرویسها"
BTN_WALLET = "💳 شارژ کیف پول"
BTN_ACCOUNT = "👤 حساب کاربری"
BTN_SUPPORT = "🎧 پشتیبانی"
MENU_TEXTS = {BTN_BUY, BTN_MY, BTN_TARIFFS, BTN_WALLET, BTN_ACCOUNT, BTN_SUPPORT}

CATEGORY_LABELS = {"economic": "اقتصادی", "vip": "VIP", "gaming": "گیمینگ"}
MAX_DISCOUNT = 90


# ---------------------------------------------------------------------------
# مدلهای دیتابیس
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    user_id = Column(BigInteger, primary_key=True, autoincrement=False)
    username = Column(String)
    first_name = Column(String)
    balance = Column(Integer, default=0)
    is_banned = Column(Boolean, default=False)
    is_new_user = Column(Boolean, default=True)
    purchase_count_current_month = Column(Integer, default=0)
    last_purchase_month = Column(String, default="None")
    has_monthly_discount = Column(Boolean, default=False)
    note = Column(Text)
    referred_by = Column(BigInteger)
    referral_rewarded = Column(Boolean, default=False)
    joined_at = Column(String)


class Service(Base):
    __tablename__ = "services"
    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String)
    name = Column(String)
    price = Column(Integer)


class UserConfig(Base):
    __tablename__ = "user_configs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    config_name = Column(String)
    config_details = Column(String)
    service_id = Column(Integer)
    expires_at = Column(String)  # YYYY-MM-DD (میلادی)
    reminded = Column(Boolean, default=False)


class Request(Base):
    __tablename__ = "requests"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    req_type = Column(String)  # buy | renew
    plan_name = Column(String)
    status = Column(String, default="unanswered")
    created_at = Column(String)
    answered_at = Column(String)
    receipt_file_id = Column(String)
    price = Column(Integer, default=0)
    paid_wallet = Column(Boolean, default=False)
    config_id = Column(Integer)
    service_id = Column(Integer)
    discount_code = Column(String)


class KV(Base):
    __tablename__ = "kv_settings"
    key = Column(String, primary_key=True)
    value = Column(Text)


class DiscountCode(Base):
    __tablename__ = "discount_codes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String, unique=True)
    percent = Column(Integer, default=0)
    expires_at = Column(String)  # YYYY-MM-DD یا خالی
    max_uses = Column(Integer, default=0)  # 0 = نامحدود
    used_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)


class CodeUse(Base):
    __tablename__ = "code_uses"
    id = Column(Integer, primary_key=True, autoincrement=True)
    code_id = Column(Integer)
    user_id = Column(BigInteger)


class Ticket(Base):
    __tablename__ = "tickets"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    status = Column(String, default="open")
    created_at = Column(String)


class TicketMessage(Base):
    __tablename__ = "ticket_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(Integer)
    sender = Column(String)  # user | staff
    text = Column(Text)
    photo_id = Column(String)
    created_at = Column(String)


class AdminLog(Base):
    __tablename__ = "admin_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    admin_id = Column(BigInteger)
    action = Column(Text)
    created_at = Column(String)


DEFAULT_PLANS = [
    ("economic", "100 گیگ - ۲ کاربر", 298000),
    ("economic", "100 گیگ - نامحدود", 398000),
    ("vip", "10 گیگ", 190000),
    ("vip", "20 گیگ", 320000),
    ("vip", "30 گیگ", 490000),
    ("vip", "50 گیگ", 790000),
    ("vip", "100 گیگ", 1330000),
    ("gaming", "10 گیگ", 220000),
    ("gaming", "20 گیگ", 360000),
    ("gaming", "30 گیگ", 530000),
    ("gaming", "50 گیگ", 850000),
]

DEFAULT_FAQ = (
    "❓ <b>سوالات متداول</b>\n\n"
    "🔹 <b>چطور وصل شوم؟</b>\nسابلینک را در برنامه V2Box اضافه کنید (آموزش داخل کانال).\n\n"
    "🔹 <b>سرویسها چند روزهاند؟</b>\nهر سرویس ۳۰ روزه است و قبل از پایان یادآوری میگیرید.\n\n"
    "🔹 <b>چطور تمدید کنم؟</b>\nاز بخش «سرویسهای من» روی سرویس بزنید و دکمه تمدید را انتخاب کنید."
)

# ---------------------------------------------------------------------------
# تنظیمات قابل تغییر از پنل (جدول key-value)
# ---------------------------------------------------------------------------
DEFAULTS = {
    "card_number": "",
    "card_holder": "",
    "support_info": "",
    "discount_percent": "0",
    "first_on": "1",
    "first_percent": "10",
    "loyalty_on": "1",
    "loyalty_percent": "10",
    "maintenance_on": "0",
    "maintenance_text": "🔧 ربات در حال بروزرسانی است. لطفاً کمی بعد دوباره تلاش کنید.",
    "welcome_text": "",
    "welcome_banner": "",
    "channel_link": "",
    "sticker_welcome": "",
    "sticker_purchase": "",
    "sticker_charge": "",
    "sticker_config": "",
    "charge_amounts": "200000,400000,600000,800000,1000000,1500000",
    "min_charge": "50000",
    "referral_on": "1",
    "referral_reward": "20000",
    "reminder_days": "3",
    "report_on": "1",
    "backup_on": "1",
    "faq_text": DEFAULT_FAQ,
    "sub_admins": "",
}

# متادیتای تنظیماتی که از منوی «تنظیمات ربات» ویرایش میشوند
SETTINGS = {
    "discount_percent": dict(page="disc", type="num", label="تخفیف عمومی (٪)", min=0, max=MAX_DISCOUNT),
    "first_on": dict(page="disc", type="toggle", label="تخفیف خرید اول"),
    "first_percent": dict(page="disc", type="num", label="درصد تخفیف خرید اول", min=0, max=50),
    "loyalty_on": dict(page="disc", type="toggle", label="تخفیف وفاداری"),
    "loyalty_percent": dict(page="disc", type="num", label="درصد تخفیف وفاداری", min=0, max=50),
    "card_number": dict(page="pay", type="text", label="شماره کارت"),
    "card_holder": dict(page="pay", type="text", label="نام صاحب کارت"),
    "charge_amounts": dict(page="pay", type="text", label="مبالغ شارژ (با کاما جدا کنید)"),
    "min_charge": dict(page="pay", type="num", label="حداقل مبلغ شارژ (تومان)", min=0, max=100000000),
    "maintenance_on": dict(page="maint", type="toggle", label="حالت تعمیر"),
    "maintenance_text": dict(page="maint", type="text", label="پیام حالت تعمیر"),
    "welcome_text": dict(page="start", type="text", label="متن خوشآمدگویی ({name} = اسم کاربر)"),
    "welcome_banner": dict(page="start", type="photo", label="بنر پیام شروع"),
    "channel_link": dict(page="start", type="text", label="لینک کانال"),
    "sticker_welcome": dict(page="start", type="sticker", label="استیکر خوشآمدگویی"),
    "sticker_purchase": dict(page="start", type="sticker", label="استیکر ثبت سفارش"),
    "sticker_charge": dict(page="start", type="sticker", label="استیکر شارژ موفق"),
    "sticker_config": dict(page="start", type="sticker", label="استیکر تحویل کانفیگ"),
    "support_info": dict(page="support", type="text", label="اطلاعات پشتیبانی (آیدی ادمین)"),
    "faq_text": dict(page="support", type="text", label="متن سوالات متداول"),
    "referral_on": dict(page="ref", type="toggle", label="سیستم دعوت دوستان"),
    "referral_reward": dict(page="ref", type="num", label="پاداش دعوت (تومان)", min=0, max=100000000),
    "reminder_days": dict(page="auto", type="num", label="یادآوری تمدید (روز مانده)", min=1, max=30),
    "report_on": dict(page="auto", type="toggle", label="گزارش روزانه برای ادمین"),
    "backup_on": dict(page="auto", type="toggle", label="بکاپ شبانه خودکار"),
}

SETTING_PAGES = {
    "disc": "🏷 تخفیفها",
    "pay": "💳 پرداخت و شارژ",
    "maint": "🔧 حالت تعمیر",
    "start": "👋 پیام شروع و استیکرها",
    "support": "🎧 پشتیبانی و سوالات متداول",
    "ref": "🎁 دعوت دوستان",
    "auto": "🔔 یادآوری و گزارش خودکار",
}

_cache = {}


def get_setting(key):
    if key in _cache:
        return _cache[key]
    with SessionLocal() as s:
        row = s.get(KV, key)
        value = row.value if row is not None and row.value is not None else DEFAULTS.get(key, "")
    _cache[key] = value
    return value


def set_setting(key, value):
    value = "" if value is None else str(value)
    with SessionLocal() as s:
        row = s.get(KV, key)
        if row is None:
            s.add(KV(key=key, value=value))
        else:
            row.value = value
        s.commit()
    _cache[key] = value


def get_int(key):
    try:
        return int(get_setting(key))
    except (TypeError, ValueError):
        return 0


def get_bool(key):
    return get_setting(key) in ("1", "true", "True")


# ---------------------------------------------------------------------------
# ساخت و مهاجرت دیتابیس
# ---------------------------------------------------------------------------
def _migrate_columns():
    wanted = {
        "users": {
            "username": "VARCHAR",
            "first_name": "VARCHAR",
            "is_new_user": "BOOLEAN DEFAULT TRUE",
            "purchase_count_current_month": "INTEGER DEFAULT 0",
            "last_purchase_month": "VARCHAR",
            "has_monthly_discount": "BOOLEAN DEFAULT FALSE",
            "note": "TEXT",
            "referred_by": "BIGINT",
            "referral_rewarded": "BOOLEAN DEFAULT FALSE",
            "joined_at": "VARCHAR",
        },
        "user_configs": {
            "service_id": "INTEGER",
            "expires_at": "VARCHAR",
            "reminded": "BOOLEAN DEFAULT FALSE",
        },
        "requests": {
            "receipt_file_id": "VARCHAR",
            "price": "INTEGER DEFAULT 0",
            "paid_wallet": "BOOLEAN DEFAULT FALSE",
            "config_id": "INTEGER",
            "service_id": "INTEGER",
            "discount_code": "VARCHAR",
        },
    }
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in wanted.items():
            existing = {c["name"].lower() for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

    if engine.dialect.name == "postgresql":
        for table in ("users", "requests", "user_configs"):
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN user_id TYPE BIGINT"))
            except Exception as e:  # noqa: BLE001
                logging.warning("BIGINT migration skipped for %s: %s", table, e)


def init_db():
    Base.metadata.create_all(engine)
    _migrate_columns()
    with SessionLocal() as s:
        if s.query(Service).count() == 0:
            s.add_all([Service(category=c, name=n, price=p) for c, n, p in DEFAULT_PLANS])

        for key, env in (("card_number", "CARD_NUMBER"), ("card_holder", "CARD_HOLDER")):
            if s.get(KV, key) is None:
                value = os.environ.get(env, "").strip()
                if not value:
                    raise SystemExit(
                        f"Missing required environment variable: {env} "
                        "(needed on first run; later it can be changed from the admin panel)."
                    )
                s.add(KV(key=key, value=value))

        if s.get(KV, "support_info") is None:
            s.add(KV(key="support_info", value=os.environ.get("SUPPORT_INFO", "").strip()))
        s.commit()
    _cache.clear()


# ---------------------------------------------------------------------------
# وضعیتهای FSM
# ---------------------------------------------------------------------------
class PaymentState(StatesGroup):
    waiting_for_receipt = State()
    waiting_for_code = State()
    waiting_for_charge_receipt = State()
    waiting_for_custom_amount = State()
    waiting_for_admin_config = State()
    broadcasting = State()
    adjusting_balance = State()
    editing_setting = State()
    editing_card_pair = State()
    private_message = State()
    editing_note = State()
    searching_user = State()
    editing_plan_price = State()
    editing_plan_name = State()
    adding_plan = State()
    creating_code = State()
    adding_subadmin = State()
    ticket_waiting_text = State()
    ticket_staff_reply = State()
    restore_wait_file = State()


# ---------------------------------------------------------------------------
# توابع کمکی عمومی
# ---------------------------------------------------------------------------
iran_timezone = timezone(timedelta(hours=3, minutes=30))


def now_iran():
    return datetime.now(iran_timezone)


def today_iran():
    return now_iran().date()


def jalali_today():
    return jdatetime.date.fromgregorian(date=today_iran())


def get_current_jalali():
    return f"{jalali_today().strftime('%Y/%m/%d')} - {now_iran().strftime('%H:%M:%S')}"


def jalali_from_iso(iso_date):
    try:
        d = date.fromisoformat(iso_date)
        return jdatetime.date.fromgregorian(date=d).strftime("%Y/%m/%d")
    except Exception:  # noqa: BLE001
        return "-"


def log_action(admin_id, action):
    try:
        with SessionLocal() as s:
            s.add(AdminLog(admin_id=admin_id, action=action, created_at=get_current_jalali()))
            s.commit()
    except Exception as e:  # noqa: BLE001
        logging.warning("log_action failed: %s", e)


def sub_admins():
    ids = []
    for part in get_setting("sub_admins").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


def is_main(user_id):
    return user_id == ADMIN_ID


def is_staff(user_id):
    return user_id == ADMIN_ID or user_id in sub_admins()


def staff_ids():
    ids = [ADMIN_ID]
    for i in sub_admins():
        if i not in ids:
            ids.append(i)
    return ids


class MainAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return bool(u and is_main(u.id))


class Staff(BaseFilter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return bool(u and is_staff(u.id))


def user_display(u):
    """نام قابل نمایش کاربر (بدون escape)."""
    parts = []
    if u.first_name:
        parts.append(u.first_name)
    if u.username:
        parts.append(f"@{u.username}")
    return " ".join(parts) if parts else str(u.user_id)


def user_label(tg_user):
    if tg_user.username:
        return f"@{esc(tg_user.username)}"
    return f"آیدی: {tg_user.id}"


def get_or_create_user(session, user_id):
    user = session.get(User, user_id)
    if user is None:
        user = User(
            user_id=user_id,
            balance=0,
            is_banned=False,
            is_new_user=True,
            purchase_count_current_month=0,
            last_purchase_month="None",
            has_monthly_discount=False,
            referral_rewarded=False,
            joined_at=today_iran().isoformat(),
        )
        session.add(user)
        session.flush()
    return user


def is_banned(user_id):
    with SessionLocal() as s:
        user = s.get(User, user_id)
        return bool(user and user.is_banned)


# ---------------------------------------------------------------------------
# تخفیفها (جمعی) و قیمتگذاری
# ---------------------------------------------------------------------------
def discount_breakdown(user_id):
    """(درصد کل, [(درصد, توضیح), ...]) برای کاربر؛ تخفیفها جمع میشوند."""
    with SessionLocal() as s:
        user = s.get(User, user_id)
        is_new = bool(user.is_new_user) if user else True
        has_monthly = bool(user.has_monthly_discount) if user else False

    first = get_int("first_percent") if (get_bool("first_on") and is_new) else 0
    loyal = get_int("loyalty_percent") if (get_bool("loyalty_on") and has_monthly) else 0

    parts = []
    # مثل نسخه اصلی: «خرید اول» یا «وفاداری»؛ اگر هر دو بود، بزرگتر اعمال میشود
    if first or loyal:
        if first >= loyal:
            parts.append((first, "تخفیف خرید اول"))
        else:
            parts.append((loyal, "تخفیف وفاداری"))
    general = get_int("discount_percent")
    if general > 0:
        parts.append((general, "تخفیف ویژه"))
    total = min(sum(p for p, _ in parts), MAX_DISCOUNT)
    return total, parts


def price_with_percent(base, percent):
    return int(base * (100 - percent) / 100)


def price_line(name, base, final):
    if final != base:
        return f"🔹 <s>{base:,}</s> {ARROW} <b>{final:,}</b> تومان ({esc(name)})\n"
    return f"🔹 {base:,} تومان ({esc(name)})\n"


def breakdown_text(total, parts, extra=None):
    if total <= 0:
        return ""
    items = [f"{p}٪ {label}" for p, label in parts]
    if extra:
        items.append(extra)
    return f"🎁 <b>تخفیف شما: {total}٪</b> ({' + '.join(items)})"


# ---------------------------------------------------------------------------
# کدهای تخفیف
# ---------------------------------------------------------------------------
def validate_code(session, code_str, user_id):
    """(DiscountCode | None, پیام خطا)"""
    code_str = (code_str or "").strip().upper()
    if not code_str:
        return None, "کد وارد نشده است."
    dc = session.query(DiscountCode).filter(DiscountCode.code == code_str).first()
    dc = session.query(DiscountCode).filter(DiscountCode.code == code_str).first()
    if dc is None or not dc.is_active:
        return None, "کد تخفیف نامعتبر است."
    if dc.expires_at:
        try:
            if date.fromisoformat(dc.expires_at) < today_iran():
                return None, "این کد منقضی شده است."
        except ValueError:
            pass
    if (dc.max_uses or 0) > 0 and (dc.used_count or 0) >= dc.max_uses:
        return None, "ظرفیت استفاده از این کد تمام شده است."
    used = (
        session.query(CodeUse).filter(CodeUse.code_id == dc.id, CodeUse.user_id == user_id).first()
    )
    if used:
        return None, "شما قبلاً از این کد استفاده کردهاید."
    return dc, ""


def consume_code(session, code_str, user_id):
    dc, err = validate_code(session, code_str, user_id)
    if dc is None:
        raise ValueError(err)
    dc.used_count = (dc.used_count or 0) + 1
    session.add(CodeUse(code_id=dc.id, user_id=user_id))


def release_code(session, code_str, user_id):
    if not code_str:
        return
    dc = session.query(DiscountCode).filter(DiscountCode.code == code_str).first()
    if dc is None:
        return
    use = session.query(CodeUse).filter(CodeUse.code_id == dc.id, CodeUse.user_id == user_id).first()
    if use:
        session.delete(use)
        dc.used_count = max(0, (dc.used_count or 0) - 1)
        print("done")

# --------------------------------------------------------------------------
# توابع کمکی پیامرسانی
# ---------------------------------------------------------------------------
async def edit_or_send(call: types.CallbackQuery, text_, markup=None):
    try:
        await call.message.edit_text(text_, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        await call.message.answer(text_, reply_markup=markup)


async def reply_to(target, text_, markup=None):
    """پیام را برای Message ارسال یا برای CallbackQuery ویرایش میکند."""
    if isinstance(target, types.CallbackQuery):
        await edit_or_send(target, text_, markup)
    else:
        await target.answer(text_, reply_markup=markup)


async def append_status(call: types.CallbackQuery, line, markup=None):
    if call.message.caption is not None:
        base = call.message.caption
    else:
        base = call.message.text or ""
    new_text = f"{base}\n\n{line}"
    try:
        if call.message.photo:
            await call.message.edit_caption(caption=new_text, reply_markup=markup, parse_mode=None)
        else:
            await call.message.edit_text(new_text, reply_markup=markup, parse_mode=None)
    except TelegramBadRequest as e:
        logging.warning("append_status failed: %s", e)


async def notify_staff(bot, text_, markup=None, photo=None):
    for cid in staff_ids():
        try:
            if photo:
                await bot.send_photo(chat_id=cid, photo=photo, caption=text_, reply_markup=markup)
            else:
                await bot.send_message(chat_id=cid, text=text_, reply_markup=markup)
        except Exception as e:  # noqa: BLE001
            logging.warning("notify_staff failed for %s: %s", cid, e)


async def send_sticker(bot, chat_id, key):
    sticker_id = get_setting(key).strip()
    if not sticker_id:
        return
    try:
        await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
    except Exception as e:  # noqa: BLE001
        logging.warning("sticker %s failed: %s", key, e)


async def relay(bot, chat_id, message: types.Message, header, markup=None):
    """متن یا عکس پیام را با یک سرتیتر برای chat_id میفرستد."""
    body = message.caption if message.photo else message.text
    full = f"{header}{esc(body or '')}"
    if message.photo:
        await bot.send_photo(chat_id=chat_id, photo=message.photo[-1].file_id, caption=full, reply_markup=markup)
    else:
        await bot.send_message(chat_id=chat_id, text=full, reply_markup=markup)


# ---------------------------------------------------------------------------
# کیبوردها
# ---------------------------------------------------------------------------
def get_user_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BUY), KeyboardButton(text=BTN_MY)],
            [KeyboardButton(text=BTN_TARIFFS), KeyboardButton(text=BTN_WALLET)],
            [KeyboardButton(text=BTN_ACCOUNT), KeyboardButton(text=BTN_SUPPORT)],
        ],
        resize_keyboard=True,
    )


def main_inline_markup():
    b = InlineKeyboardButton
    rows = [
        [b(text=BTN_BUY, callback_data="main_buy")],
        [b(text=BTN_TARIFFS, callback_data="tariffs"), b(text=BTN_WALLET, callback_data="wallet_menu")],
        [b(text=BTN_MY, callback_data="my_services_callback"), b(text=BTN_ACCOUNT, callback_data="back_to_account")],
        [b(text=BTN_SUPPORT, callback_data="support_menu"), b(text="🎁 دعوت دوستان", callback_data="referral_menu")],
    ]
    ch = get_setting("channel_link").strip()
    url = None
    if ch.startswith("@"):
        url = f"https://t.me/{ch[1:]}"
    elif ch.startswith("http://") or ch.startswith("https://"):
        url = ch
    elif ch.startswith("t.me/"):
        url = f"https://{ch}"
    if url:
        rows.append([b(text="📢 کانال ما", url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_home_markup():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="main_menu")]]
    )


def get_admin_main_keyboard(user_id=None):
    b = InlineKeyboardButton
    if user_id is not None and not is_main(user_id):
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    b(text="🟢 پاسخ داده نشده", callback_data="admin_unanswered"),
                    b(text="🔴 پاسخ داده شده", callback_data="admin_answered"),
                ],
                [b(text="🎫 تیکتها", callback_data="admin_tickets")],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                b(text="🟢 پاسخ داده نشده", callback_data="admin_unanswered"),
                b(text="🔴 پاسخ داده شده", callback_data="admin_answered"),
            ],
            [b(text="👥 مدیریت کاربران", callback_data="admin_users"), b(text="🎫 تیکتها", callback_data="admin_tickets")],
            [b(text="📊 آمار کلی فروش", callback_data="admin_sales_stats"), b(text="🧾 لاگ فعالیتها", callback_data="admin_logs")],
            [b(text="📦 مدیریت پلنها", callback_data="admin_plans"), b(text="🏷 کدهای تخفیف", callback_data="admin_codes")],
            [b(text="📢 پیام همگانی", callback_data="admin_broadcast_start"), b(text="🧰 بکاپ", callback_data="admin_backup")],
            [b(text="⚙ تنظیمات ربات", callback_data="admin_settings")],
        ]
    )


def admin_request_markup(req_id):
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                b(text="✅ تایید", callback_data=f"admin_confirm_{req_id}"),
                b(text="❌ رد درخواست", callback_data=f"admin_reject_{req_id}"),
            ],
            [b(text="⚙ پنل مدیریت", callback_data="admin_home")],
        ]
    )


# ---------------------------------------------------------------------------
# میدلور: ثبت کاربر، مسدودسازی، حالت تعمیر، ریاکشن
# ---------------------------------------------------------------------------
def touch_user(tg_user):
    """کاربر را ثبت/بهروزرسانی میکند. اگر کاربر تازه ساخته شد True برمیگرداند."""
    try:
        with SessionLocal() as s:
            created = s.get(User, tg_user.id) is None
            u = get_or_create_user(s, tg_user.id)
            u.username = tg_user.username
            u.first_name = tg_user.first_name
            s.commit()
            return created
    except Exception as e:  # noqa: BLE001
        logging.warning("touch_user failed: %s", e)
        return False


async def _react(event):
    try:
        if isinstance(event, types.Message):
            if event.photo or (event.text and event.text in MENU_TEXTS):
                await event.bot.set_message_reaction(
                    chat_id=event.chat.id, message_id=event.message_id,
                    reaction=[ReactionTypeEmoji(emoji="👍")],
                )
        elif isinstance(event, types.CallbackQuery) and event.message:
            await event.bot.set_message_reaction(
                chat_id=event.message.chat.id, message_id=event.message.message_id,
                reaction=[ReactionTypeEmoji(emoji="👍")],
            )
    except Exception:  # noqa: BLE001
        pass


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.is_bot:
            return await handler(event, data)

        data["is_new_user_row"] = touch_user(user)

        if not is_staff(user.id):
            if is_banned(user.id):
                if isinstance(event, types.CallbackQuery):
                    await event.answer("دسترسی شما مسدود شده است.", show_alert=True)
                return None
            if get_bool("maintenance_on"):
                msg = get_setting("maintenance_text") or "ربات در حال بروزرسانی است."
                if isinstance(event, types.CallbackQuery):
                    await event.answer(msg[:190], show_alert=True)
                else:
                    await event.answer(msg)
                return None
            asyncio.create_task(_react(event))
        return await handler(event, data)


dp.message.outer_middleware(AccessMiddleware())
dp.callback_query.outer_middleware(AccessMiddleware())

BOT_USERNAME = ""


# ---------------------------------------------------------------------------
# پیام شروع و منوی اصلی
# ---------------------------------------------------------------------------
def expiring_list(session, user_id, days):
    out = []
    rows = (
        session.query(UserConfig)
        .filter(UserConfig.user_id == user_id, UserConfig.expires_at.isnot(None))
        .all()
    )
    for c in rows:
        try:
            left = (date.fromisoformat(c.expires_at) - today_iran()).days
        except (TypeError, ValueError):
            continue
        if left <= days:
            out.append((c.config_name, left))
    return out


def build_welcome(user_id, first_name):
    name = first_name or "دوست عزیز"
    custom = get_setting("welcome_text").strip()
    with SessionLocal() as s:
        u = s.get(User, user_id)
        balance = (u.balance if u else 0) or 0
        is_new = bool(u.is_new_user) if u else True
        active = s.query(func.count(UserConfig.id)).filter(UserConfig.user_id == user_id).scalar()
        expiring = expiring_list(s, user_id, get_int("reminder_days"))

    if custom:
        body = esc(custom.replace("{name}", name))
    elif is_new:
        body = f"سلام {esc(name)} عزیز 👋\nبه Swan VPN خوش آمدی!"
    else:
        body = (
            f"سلام {esc(name)} عزیز 👋\nخوش برگشتی!\n\n"
            f"💳 موجودی: {balance:,} تومان\n📦 سرویسهای فعال: {active}"
        )

    total, parts = discount_breakdown(user_id)
    if total > 0:
        body += "\n\n" + breakdown_text(total, parts)

    for cname, left in expiring[:3]:
        if left >= 0:
            body += f"\n⏰ سرویس «{esc(cname or '')}» {left} روز دیگر تمام میشود."
        else:
            body += f"\n⚠ سرویس «{esc(cname or '')}» منقضی شده است."

    body += "\n\nبرای بازگشت به منوی اصلی، /start را بزنید."
    return body


@dp.message(Command("start"))
async def show_main_menu(
    message: types.Message, state: FSMContext, command: CommandObject, is_new_user_row: bool = False
):
    await state.clear()

    # دعوت دوستان: لینک /start ref_<id>
    if is_new_user_row and get_bool("referral_on") and command.args and command.args.startswith("ref_"):
        try:
            ref_id = int(command.args[4:])
        except ValueError:
            ref_id = 0
        if ref_id and ref_id != message.from_user.id:
            with SessionLocal() as s:
                if s.get(User, ref_id) is not None:
                    me = s.get(User, message.from_user.id)
                    if me is not None and not me.referred_by:
                        me.referred_by = ref_id
                        s.commit()

    await send_sticker(message.bot, message.chat.id, "sticker_welcome")
    body = build_welcome(message.from_user.id, message.from_user.first_name)
    markup = main_inline_markup()
    banner = get_setting("welcome_banner").strip()
    sent = False
    if banner:
        try:
            await message.answer_photo(photo=banner, caption=body, reply_markup=markup)
            sent = True
        except Exception as e:  # noqa: BLE001
            logging.warning("banner send failed: %s", e)
    if not sent:
        await message.answer(body, reply_markup=markup)
    await message.answer("🔽 منوی پایین هم همیشه در دسترس شماست.", reply_markup=get_user_keyboard())


@dp.callback_query(F.data == "main_menu")
async def main_menu_handler_call(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await edit_or_send(
        call, build_welcome(call.from_user.id, call.from_user.first_name), main_inline_markup()
    )


# ---------------------------------------------------------------------------
# تعرفهها
# ---------------------------------------------------------------------------
def render_tariffs(user_id):
    total, parts = discount_breakdown(user_id)
    with SessionLocal() as s:
        rows = [(r.category, r.name, r.price) for r in s.query(Service).order_by(Service.category, Service.id).all()]

    out = "📊 <b>لیست تعرفه و قیمتهای ویژه شما:</b>\n"
    if total > 0:
        out += "\n" + breakdown_text(total, parts) + "\n"
    current = None
    for cat, name, price in rows:
        if cat != current:
            current = cat
            out += f"\n📂 <b>دستهبندی {esc(CATEGORY_LABELS.get(cat, cat))}:</b>\n"
        out += price_line(name, price, price_with_percent(price, total))
    return out


@dp.message(F.text == BTN_TARIFFS)
async def tariffs_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(render_tariffs(message.from_user.id), reply_markup=back_home_markup())


@dp.callback_query(F.data == "tariffs")
async def tariffs_callback(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await edit_or_send(call, render_tariffs(call.from_user.id), back_home_markup())


# ---------------------------------------------------------------------------
# خرید
# ---------------------------------------------------------------------------
def buy_menu_text(user_id):
    total, parts = discount_breakdown(user_id)
    out = "لطفاً دستهبندی مورد نظر را انتخاب کنید:"
    if total > 0:
        out += "\n\n" + breakdown_text(total, parts)
    if get_bool("loyalty_on"):
        out += f"\n💎 با ۲ خرید در هر ماه، {get_int('loyalty_percent')}٪ تخفیف وفاداری برای ماه بعد فعال میشود."
    return out


def buy_menu_markup():
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="🌱 سرویس اقتصادی", callback_data="cat_economic")],
            [b(text="💎 سرویس ویآیپی", callback_data="cat_vip")],
            [b(text="🎮 سرویس گیمینگ", callback_data="cat_gaming")],
            [b(text="🏠 بازگشت به منوی اصلی", callback_data="main_menu")],
        ]
    )


@dp.message(F.text == BTN_BUY)
async def message_buy_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(buy_menu_text(message.from_user.id), reply_markup=buy_menu_markup())


@dp.callback_query(F.data == "main_buy")
async def main_buy_handler(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await edit_or_send(call, buy_menu_text(call.from_user.id), buy_menu_markup())


@dp.callback_query(F.data.startswith("cat_"))
async def category_handler(call: types.CallbackQuery):
    await call.answer()
    category = call.data.split("_", 1)[1]
    total, parts = discount_breakdown(call.from_user.id)
    with SessionLocal() as s:
        services = [
            (r.id, r.name, r.price)
            for r in s.query(Service).filter(Service.category == category).order_by(Service.id).all()
        ]

    out = f"📊 لیست پلنهای {esc(CATEGORY_LABELS.get(category, category))}:\n"
    if total > 0:
        out += "\n" + breakdown_text(total, parts) + "\n"
    out += "\n"
    keyboard = []
    for sid, name, price in services:
        out += price_line(name, price, price_with_percent(price, total))
        keyboard.append([InlineKeyboardButton(text=f"خرید {name}", callback_data=f"buy_{sid}")])
    if not services:
        out += "فعلاً پلنی در این دسته وجود ندارد."
    keyboard.append([InlineKeyboardButton(text="⬅ برگشت به منوی خرید", callback_data="main_buy")])
    await edit_or_send(call, out, InlineKeyboardMarkup(inline_keyboard=keyboard))


def compute_checkout(user_id, data):
    sid = data.get("service_id")
    if not sid:
        return None
    with SessionLocal() as s:
        svc = s.get(Service, sid)
        if svc is None:
            return None
        label = f"{CATEGORY_LABELS.get(svc.category, svc.category)} - {svc.name}"
        base = svc.price
        code, code_pct = None, 0
        if data.get("code"):
            dc, _err = validate_code(s, data["code"], user_id)
            if dc is not None:
                code, code_pct = dc.code, dc.percent or 0
    total, parts = discount_breakdown(user_id)
    percent = min(total + code_pct, MAX_DISCOUNT)
    return dict(
        service_id=sid, label=label, base=base, percent=percent, parts=parts,
        code=code, code_pct=code_pct, final=price_with_percent(base, percent),
        renew_cfg_id=data.get("renew_cfg_id"),
    )


def checkout_view(co):
    st = get_setting("card_number"), get_setting("card_holder")
    if co["final"] != co["base"]:
        price_display = f"<s>{co['base']:,}</s> {ARROW} <b>{co['final']:,}</b> تومان"
    else:
        price_display = f"{co['base']:,} تومان"
    title = "🔄 تمدید سرویس" if co["renew_cfg_id"] else "🛒 سفارش شما"
    out = f"{title}: {esc(co['label'])}\n💰 قیمت: {price_display}\n"
    if co["percent"] > 0:
        extra = f"{co['code_pct']}٪ کد تخفیف" if co["code"] else None
        out += breakdown_text(co["percent"], co["parts"], extra) + "\n"
    out += (
        "\nشما میتوانید از طریق کارت یا موجودی کیف پول پرداخت کنید.\n\n"
        f"💳 شماره کارت جهت واریز:\n<code>{esc(st[0])}</code>\n"
        f"به نام: {esc(st[1])}\n\n"
        "پس از واریز فیش را ارسال کنید یا در صورت داشتن موجودی کافی از دکمه زیر استفاده کنید."
    )
    b = InlineKeyboardButton
    code_btn = (
        b(text="❌ حذف کد تخفیف", callback_data="remove_code")
        if co["code"]
        else b(text="🏷 کد تخفیف دارم", callback_data="use_code")
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="✅ ارسال رسید واریز", callback_data="confirm_payment")],
            [b(text="💳 پرداخت از کیف پول", callback_data="pay_with_wallet")],
            [code_btn],
            [b(text="⬅ انصراف و بازگشت", callback_data="main_buy")],
        ]
    )
    return out, markup


async def send_checkout(target, state, user_id):
    data = await state.get_data()
    co = compute_checkout(user_id, data)
    if co is None:
        await state.clear()
        await reply_to(target, "این سفارش منقضی شده است. لطفاً دوباره از منوی خرید شروع کنید.", back_home_markup())
        return
    out, markup = checkout_view(co)
    await reply_to(target, out, markup)


@dp.callback_query(F.data.startswith("buy_"))
async def buy_plan_handler(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    try:
        service_id = int(call.data.split("_", 1)[1])
    except ValueError:
        return
    await state.clear()
    await state.set_state(PaymentState.waiting_for_receipt)
    await state.update_data(service_id=service_id, renew_cfg_id=None, code=None)
    await send_checkout(call, state, call.from_user.id)


@dp.callback_query(F.data.startswith("renew_"))
async def renew_handler(call: types.CallbackQuery, state: FSMContext):
    try:
        cfg_id = int(call.data.split("_", 1)[1])
    except ValueError:
        return
    with SessionLocal() as s:
        cfg = s.get(UserConfig, cfg_id)
        if cfg is None or cfg.user_id != call.from_user.id:
            await call.answer("سرویس یافت نشد.", show_alert=True)
            return
        service_id = cfg.service_id
        exists = bool(service_id and s.get(Service, service_id))
    if not exists:
        await call.answer("تمدید خودکار برای این سرویس ممکن نیست؛ با پشتیبانی تماس بگیرید.", show_alert=True)
        return
    await call.answer()
    await state.clear()
    await state.set_state(PaymentState.waiting_for_receipt)
    await state.update_data(service_id=service_id, renew_cfg_id=cfg_id, code=None)
    await send_checkout(call, state, call.from_user.id)


@dp.callback_query(F.data == "use_code")
async def use_code_handler(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    if not data.get("service_id"):
        await edit_or_send(call, "این سفارش منقضی شده است. لطفاً دوباره شروع کنید.", back_home_markup())
        return
    await state.set_state(PaymentState.waiting_for_code)
    await edit_or_send(call, "🏷 کد تخفیف خود را وارد کنید:")


@dp.message(PaymentState.waiting_for_code)
async def code_entered_handler(message: types.Message, state: FSMContext):
    with SessionLocal() as s:
        dc, err = validate_code(s, message.text or "", message.from_user.id)
        code = dc.code if dc else None
    if code is None:
        await message.answer(f"❌ {err}\nدوباره وارد کنید یا از منوی پایین خارج شوید.")
        return
    await state.update_data(code=code)
    await state.set_state(PaymentState.waiting_for_receipt)
    await send_checkout(message, state, message.from_user.id)


@dp.callback_query(F.data == "remove_code")
async def remove_code_handler(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(code=None)
    await send_checkout(call, state, call.from_user.id)


def build_request(session, user_id, co, paid_wallet, receipt_id):
    if co["code"]:
        consume_code(session, co["code"], user_id)  # ValueError اگر دیگر معتبر نباشد
    req = Request(
        user_id=user_id,
        req_type="renew" if co["renew_cfg_id"] else "buy",
        plan_name=co["label"],
        status="unanswered",
        created_at=get_current_jalali(),
        receipt_file_id=receipt_id,
        price=co["final"],
        paid_wallet=paid_wallet,
        config_id=co["renew_cfg_id"],
        service_id=co["service_id"],
        discount_code=co["code"],
    )
    session.add(req)
    session.flush()
    return req


def request_caption(req_id, tg_user, co, req_type, via_wallet):
    kind = "تمدید" if req_type == "renew" else "خرید"
    way = "با کیف پول" if via_wallet else "با رسید"
    out = (
        f"🚨 درخواست {kind} جدید {way} (کد #{req_id})\n"
        f"👤 کاربر: {user_label(tg_user)} ({tg_user.id})\n"
        f"📦 پلن: {esc(co['label'])}\n"
        f"💰 مبلغ: {co['final']:,} تومان\n"
    )
    if co["code"]:
        out += f"🏷 کد تخفیف: {esc(co['code'])}\n"
    return out + f"⏱ زمان ثبت: {get_current_jalali()}"


@dp.callback_query(F.data == "pay_with_wallet")
async def pay_with_wallet_handler(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    co = compute_checkout(call.from_user.id, data)
    if co is None:
        await call.answer("این سفارش منقضی شده است. لطفاً دوباره شروع کنید.", show_alert=True)
        return

    user_id = call.from_user.id
    with SessionLocal() as s:
        user = get_or_create_user(s, user_id)
        balance = user.balance or 0
        if balance < co["final"]:
            await call.answer(
                f"موجودی شما ({balance:,} تومان) برای پرداخت این سرویس کافی نیست.", show_alert=True
            )
            return
        try:
            req = build_request(s, user_id, co, True, "None")
        except ValueError as e:
            await call.answer(str(e), show_alert=True)
            return
        user.balance = balance - co["final"]
        req_id, req_type = req.id, req.req_type
        s.commit()

    await call.answer()
    await state.clear()
    await edit_or_send(call, "✅ پرداخت با موفقیت از کیف پول انجام شد. لطفاً منتظر بررسی ادمین باشید. 🙏")
    await send_sticker(call.bot, call.message.chat.id, "sticker_purchase")
    await notify_staff(call.bot, request_caption(req_id, call.from_user, co, req_type, True), admin_request_markup(req_id))


@dp.callback_query(F.data == "confirm_payment")
async def confirm_payment_handler(call: types.CallbackQuery):
    await call.answer()
    await edit_or_send(call, "لطفاً اسکرینشات یا عکس رسید واریزی خود را در چت بفرستید تا بررسی شود.")


@dp.message(PaymentState.waiting_for_receipt, F.photo)
async def receipt_handler(message: types.Message, state: FSMContext):
    data = await state.get_data()
    co = compute_checkout(message.from_user.id, data)
    if co is None:
        await state.clear()
        await message.answer("این سفارش منقضی شده است. لطفاً دوباره از منوی خرید شروع کنید.")
        return

    user_id = message.from_user.id
    photo_id = message.photo[-1].file_id
    with SessionLocal() as s:
        get_or_create_user(s, user_id)
        try:
            req = build_request(s, user_id, co, False, photo_id)
        except ValueError as e:
            await message.answer(f"❌ {e}")
            return
        req_id, req_type = req.id, req.req_type
        s.commit()

    await state.clear()
    await message.answer(
        f"رسید شما در تاریخ {get_current_jalali()} دریافت شد و به زودی بررسی میشود. با تشکر از خرید شما! 🙏"
    )
    await send_sticker(message.bot, message.chat.id, "sticker_purchase")
    await notify_staff(
        message.bot, request_caption(req_id, message.from_user, co, req_type, False),
        admin_request_markup(req_id), photo=photo_id,
    )


@dp.message(PaymentState.waiting_for_receipt)
@dp.message(PaymentState.waiting_for_charge_receipt)
async def receipt_not_photo(message: types.Message):
    await message.answer("لطفاً رسید را به صورت عکس (اسکرینشات) ارسال کنید.")


#---------------------------------------------------------------------------
# کیف پول
# ---------------------------------------------------------------------------
def charge_amounts():
    out = []
    for part in re.split(r"[,\s،]+", get_setting("charge_amounts")):
        if part.isdigit() and int(part) > 0:
            out.append(int(part))
    return out or [200000, 400000, 600000]


async def _render_wallet(target):
    with SessionLocal() as s:
        user = s.get(User, target.from_user.id)
        balance = (user.balance if user else 0) or 0
    out = (
        "💳 <b>شارژ کیف پول Swan VPN</b>\n\n"
        f"💰 موجودی فعلی: {balance:,} تومان\n\n"
        "لطفاً مبلغ مورد نظر خود را انتخاب کنید:"
    )
    b = InlineKeyboardButton
    amounts = [a for a in charge_amounts() if a >= get_int("min_charge")]
    buttons = [b(text=f"{a:,} تومان", callback_data=f"charge_{a}") for a in amounts]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([b(text="✍ مبلغ دلخواه", callback_data="charge_custom")])
    rows.append([b(text="🏠 بازگشت به منوی اصلی", callback_data="main_menu")])
    await reply_to(target, out, InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(F.text == BTN_WALLET)
async def wallet_menu_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await _render_wallet(message)


@dp.callback_query(F.data == "wallet_menu")
async def wallet_menu_callback(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await _render_wallet(call)


async def send_charge_instructions(target, state, amount):
    await state.update_data(charge_amount=amount)
    out = (
        "💳 <b>درخواست شارژ کیف پول</b>\n"
        f"💰 مبلغ انتخاب شده: {amount:,} تومان\n\n"
        "لطفاً مبلغ را به شماره کارت زیر واریز نمایید:\n"
        f"💳 <code>{esc(get_setting('card_number'))}</code>\n"
        f"به نام: {esc(get_setting('card_holder'))}\n\n"
        "پس از واریز، روی دکمه زیر کلیک کرده و رسید خود را ارسال کنید."
    )
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="📤 ارسال رسید واریز", callback_data="send_charge_receipt")],
            [b(text="🏠 بازگشت به منوی اصلی", callback_data="main_menu")],
        ]
    )
    await reply_to(target, out, markup)


@dp.callback_query(F.data == "charge_custom")
async def charge_custom_handler(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(PaymentState.waiting_for_custom_amount)
    await edit_or_send(
        call, f"✍ مبلغ دلخواه را به تومان و به صورت عدد بفرستید (حداقل {get_int('min_charge'):,} تومان):"
    )


@dp.message(PaymentState.waiting_for_custom_amount)
async def charge_custom_amount(message: types.Message, state: FSMContext):
    raw = (message.text or "").replace(",", "").replace("،", "").strip()
    if not raw.isdigit():
        await message.answer("لطفاً فقط عدد بفرستید.")
        return
    amount = int(raw)
    if amount < get_int("min_charge") or amount > 100000000:
        await message.answer(f"مبلغ باید حداقل {get_int('min_charge'):,} تومان باشد.")
        return
    await state.set_state(None)
    await send_charge_instructions(message, state, amount)


@dp.callback_query(F.data.regexp(r"^charge_\d+$"))
async def select_charge_amount_handler(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    amount = int(call.data.split("_")[1])
    if amount < get_int("min_charge"):
        await call.answer("این مبلغ کمتر از حداقل شارژ است.", show_alert=True)
        return
    await send_charge_instructions(call, state, amount)


@dp.callback_query(F.data == "send_charge_receipt")
async def prompt_charge_receipt_handler(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    if not data.get("charge_amount"):
        await edit_or_send(call, "لطفاً ابتدا مبلغ شارژ را انتخاب کنید.", back_home_markup())
        return
    await state.set_state(PaymentState.waiting_for_charge_receipt)
    await edit_or_send(call, "لطفاً اسکرینشات یا عکس رسید واریزی خود را در چت ارسال کنید:")


@dp.message(PaymentState.waiting_for_charge_receipt, F.photo)
async def process_charge_receipt_handler(message: types.Message, state: FSMContext):
    data = await state.get_data()
    amount = data.get("charge_amount", 0)
    if not amount:
        await state.clear()
        await message.answer("درخواست منقضی شده است. لطفاً دوباره مبلغ شارژ را انتخاب کنید.")
        return

    user_id = message.from_user.id
    photo_id = message.photo[-1].file_id
    with SessionLocal() as s:
        get_or_create_user(s, user_id)
        req = Request(
            user_id=user_id, req_type="charge", plan_name="شارژ کیف پول", status="unanswered",
            created_at=get_current_jalali(), receipt_file_id=photo_id, price=amount, paid_wallet=False,
        )
        s.add(req)
        s.flush()
        req_id = req.id
        s.commit()

    await state.clear()
    await message.answer("🙏 رسید شما دریافت شد و در انتظار بررسی و تایید ادمین است. از صبر شما سپاسگزاریم.")
    caption = (
        f"🚨 <b>درخواست جدید شارژ کیف پول</b> (کد #{req_id})\n"
        f"👤 کاربر: {user_label(message.from_user)} ({user_id})\n"
        f"💰 مبلغ: {amount:,} تومان\n"
        f"⏱ زمان ارسال: {get_current_jalali()}"
    )
    await notify_staff(message.bot, caption, admin_request_markup(req_id), photo=photo_id)


# ---------------------------------------------------------------------------
# سرویسهای من
# ---------------------------------------------------------------------------
def _my_services_view(user_id):
    with SessionLocal() as s:
        configs = [
            (c.id, c.config_name, c.expires_at)
            for c in s.query(UserConfig).filter(UserConfig.user_id == user_id).all()
        ]
    if not configs:
        return "❌ شما در حال حاضر سرویس فعالی ندارید.", back_home_markup()

    buttons = []
    for cfg_id, name, exp in configs:
        suffix = ""
        if exp:
            try:
                left = (date.fromisoformat(exp) - today_iran()).days
                suffix = f" ({left} روز)" if left >= 0 else " (منقضی)"
            except ValueError:
                pass
        buttons.append(InlineKeyboardButton(text=f"🌐 {name}{suffix}", callback_data=f"view_cfg_{cfg_id}"))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="main_menu")])
    return (
        "📦 <b>سرویسهای فعال شما (برای مشاهده جزئیات انتخاب کنید):</b>",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.message(F.text == BTN_MY)
async def my_services_handler(message: types.Message, state: FSMContext):
    await state.clear()
    out, markup = _my_services_view(message.from_user.id)
    await message.answer(out, reply_markup=markup)


@dp.callback_query(F.data == "my_services_callback")
async def my_services_callback_handler(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    out, markup = _my_services_view(call.from_user.id)
    await edit_or_send(call, out, markup)


@dp.callback_query(F.data.startswith("view_cfg_"))
async def view_config_detail_handler(call: types.CallbackQuery):
    cfg_id = int(call.data.split("_")[2])
    with SessionLocal() as s:
        cfg = s.get(UserConfig, cfg_id)
        if cfg is None or cfg.user_id != call.from_user.id:
            await call.answer("سرویس یافت نشد.", show_alert=True)
            return
        name, details, exp, sid = cfg.config_name, cfg.config_details, cfg.expires_at, cfg.service_id

    await call.answer()
    out = f"📋 <b>جزئیات سرویس {esc(name or '')}:</b>\n\n<code>{esc(details or '')}</code>"
    if exp:
        out += f"\n\n📅 تاریخ پایان: {jalali_from_iso(exp)}"
    b = InlineKeyboardButton
    rows = []
    if sid:
        rows.append([b(text="🔄 تمدید این سرویس", callback_data=f"renew_{cfg_id}")])
    rows.append([b(text="⬅ بازگشت به لیست سرویسها", callback_data="my_services_callback")])
    rows.append([b(text="🏠 بازگشت به منوی اصلی", callback_data="main_menu")])
    await edit_or_send(call, out, InlineKeyboardMarkup(inline_keyboard=rows))


# ---------------------------------------------------------------------------
# حساب کاربری، تاریخچه، دعوت دوستان
# ---------------------------------------------------------------------------
def _account_view(user_id):
    with SessionLocal() as s:
        user = s.get(User, user_id)
        balance = (user.balance if user else 0) or 0
        count = (user.purchase_count_current_month if user else 0) or 0
        has_monthly = bool(user and user.has_monthly_discount)
        active_count = s.query(func.count(UserConfig.id)).filter(UserConfig.user_id == user_id).scalar()

    if has_monthly:
        status_text = f"✅ فعال ({get_int('loyalty_percent')}٪ تخفیف)"
    else:
        status_text = f"⏳ در حال حاضر {count} خرید در این ماه (نیاز به ۲ خرید)"

    out = (
        "👤 <b>حساب کاربری</b>\n\n"
        f"🆔 آیدی عددی: <code>{user_id}</code>\n"
        f"💳 موجودی کیف پول: {balance:,} تومان\n"
        f"🟢 تعداد سرویسهای فعال: {active_count}\n"
        f"🎁 وضعیت آفر وفاداری: {status_text}"
    )
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="📜 تاریخچه تراکنشها", callback_data="tx_history")],
            [b(text="🎁 دعوت دوستان", callback_data="referral_menu")],
            [b(text="🏠 بازگشت به منوی اصلی", callback_data="main_menu")],
        ]
    )
    return out, markup


@dp.message(F.text == BTN_ACCOUNT)
async def account_handler(message: types.Message, state: FSMContext):
    await state.clear()
    out, markup = _account_view(message.from_user.id)
    await message.answer(out, reply_markup=markup)


@dp.callback_query(F.data == "back_to_account")
async def back_to_account_handler(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    out, markup = _account_view(call.from_user.id)
    await edit_or_send(call, out, markup)
    
    
TYPE_LABELS = {"buy": "خرید", "renew": "تمدید", "charge": "شارژ"}


def render_history(user_id, limit=15):
    with SessionLocal() as s:
        reqs = [
            (r.id, r.req_type, r.plan_name, r.status, r.created_at, r.price or 0)
            for r in s.query(Request).filter(Request.user_id == user_id).order_by(Request.id.desc()).limit(limit).all()
        ]
    lines = []
    for req_id, rtype, plan, status, created, price in reqs:
        emoji = {"answered": "✅", "rejected": "❌"}.get(status, "⏳")
        lines.append(
            f"🔹 #{req_id} | {TYPE_LABELS.get(rtype, rtype or '')} | {esc(plan or '')} | {price:,} | {emoji}\n"
            f"⏱ {created}\n-----------------------------------"
        )
    return lines


@dp.callback_query(F.data == "tx_history")
async def transaction_history_handler(call: types.CallbackQuery):
    lines = render_history(call.from_user.id)
    if not lines:
        await call.answer("هیچ فاکتور یا درخواستی ثبت نشده است.", show_alert=True)
        return
    await call.answer()
    out = "📜 <b>تاریخچه تراکنشها و درخواستهای شما:</b>\n\n" + "\n".join(lines)
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="⬅ برگشت به حساب کاربری", callback_data="back_to_account")],
            [b(text="🏠 بازگشت به منوی اصلی", callback_data="main_menu")],
        ]
    )
    await edit_or_send(call, out, markup)


@dp.callback_query(F.data == "referral_menu")
async def referral_menu_handler(call: types.CallbackQuery):
    await call.answer()
    if not get_bool("referral_on"):
        await edit_or_send(call, "سیستم دعوت دوستان فعلاً غیرفعال است.", back_home_markup())
        return
    uid = call.from_user.id
    with SessionLocal() as s:
        invited = s.query(func.count(User.user_id)).filter(User.referred_by == uid).scalar()
        rewarded = (
            s.query(func.count(User.user_id)).filter(User.referred_by == uid, User.referral_rewarded.is_(True)).scalar()
        )
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}" if BOT_USERNAME else "—"
    out = (
        "🎁 <b>دعوت دوستان</b>\n\n"
        f"با هر دوستی که با لینک شما وارد شود و اولین خریدش تایید شود، "
        f"<b>{get_int('referral_reward'):,} تومان</b> به کیف پول شما اضافه میشود.\n\n"
        f"🔗 لینک اختصاصی شما:\n<code>{esc(link)}</code>\n\n"
        f"👥 دعوتشدهها: {invited}\n✅ پاداش گرفتهشده: {rewarded}"
    )
    await edit_or_send(call, out, back_home_markup())


#---------------------------------------------------------------------------
# پشتیبانی، سوالات متداول و تیکت (سمت کاربر)
# ---------------------------------------------------------------------------
def support_view():
    support = get_setting("support_info").strip()
    out = "🎧 <b>پشتیبانی Swan VPN</b>\n\n"
    if support:
        out += f"برای ارتباط مستقیم با ادمین به آیدی زیر پیام دهید:\n👉 {esc(support)}\n\n"
    out += "یا از دکمه زیر تیکت بفرستید تا همینجا جواب بگیرید."
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="🎫 ارسال تیکت", callback_data="ticket_new")],
            [b(text="❓ سوالات متداول", callback_data="faq_show")],
            [b(text="🏠 بازگشت به منوی اصلی", callback_data="main_menu")],
        ]
    )
    return out, markup


@dp.message(F.text == BTN_SUPPORT)
async def support_handler(message: types.Message, state: FSMContext):
    await state.clear()
    out, markup = support_view()
    await message.answer(out, reply_markup=markup)


@dp.callback_query(F.data == "support_menu")
async def support_callback(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    out, markup = support_view()
    await edit_or_send(call, out, markup)


@dp.callback_query(F.data == "faq_show")
async def faq_show(call: types.CallbackQuery):
    await call.answer()
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="🎫 ارسال تیکت", callback_data="ticket_new")],
            [b(text="⬅ بازگشت", callback_data="support_menu")],
        ]
    )
    faq = get_setting("faq_text")
    # متن پیشفرض HTML است؛ متن ساختهشده توسط ادمین escape میشود
    await edit_or_send(call, faq if faq == DEFAULT_FAQ else esc(faq), markup)


@dp.callback_query(F.data == "ticket_new")
async def ticket_new(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    with SessionLocal() as s:
        open_count = (
            s.query(func.count(Ticket.id))
            .filter(Ticket.user_id == call.from_user.id, Ticket.status == "open")
            .scalar()
        )
    if open_count >= 3:
        await edit_or_send(call, "شما ۳ تیکت باز دارید. لطفاً تا پاسخ پشتیبانی صبر کنید.", back_home_markup())
        return
    await state.set_state(PaymentState.ticket_waiting_text)
    await state.update_data(ticket_id=None)
    await edit_or_send(call, "✍ پیام یا مشکل خود را بنویسید (میتوانید عکس هم بفرستید):")


@dp.callback_query(F.data.startswith("ticket_ureply_"))
async def ticket_user_reply(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    tid = int(call.data.split("_")[2])
    with SessionLocal() as s:
        t = s.get(Ticket, tid)
        if t is None or t.user_id != call.from_user.id:
            return
    await state.set_state(PaymentState.ticket_waiting_text)
    await state.update_data(ticket_id=tid)
    await call.message.answer(f"✍ پاسخ شما برای تیکت #{tid} را بنویسید:")

@dp.message(PaymentState.ticket_waiting_text)
async def ticket_text_received(message: types.Message, state: FSMContext):
    body = message.caption if message.photo else message.text
    if not body and not message.photo:
        await message.answer("لطفاً متن یا عکس بفرستید.")
        return
    data = await state.get_data()
    tid = data.get("ticket_id")
    photo_id = message.photo[-1].file_id if message.photo else None
    with SessionLocal() as s:
        if tid:
            t = s.get(Ticket, tid)
            if t is None or t.user_id != message.from_user.id:
                await state.clear()
                return
            t.status = "open"
        else:
            t = Ticket(user_id=message.from_user.id, status="open", created_at=get_current_jalali())
            s.add(t)
            s.flush()
            tid = t.id
        s.add(TicketMessage(ticket_id=tid, sender="user", text=body or "", photo_id=photo_id, created_at=get_current_jalali()))
        s.commit()

    await state.clear()
    await message.answer(f"✅ تیکت #{tid} ثبت شد. پاسخ پشتیبانی همینجا برایتان ارسال میشود.")

    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                b(text="💬 پاسخ", callback_data=f"ticket_reply_{tid}"),
                b(text="✅ بستن تیکت", callback_data=f"ticket_close_{tid}"),
            ]
        ]
    )
    header = f"🎫 تیکت #{tid} از {user_label(message.from_user)} ({message.from_user.id}):\n\n"
    for cid in staff_ids():
        try:
            await relay(message.bot, cid, message, header, markup)
        except Exception as e:  # noqa: BLE001
            logging.warning("ticket notify failed: %s", e)