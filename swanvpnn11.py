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
    select,
    or_,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
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
# هیچ اطلاعات حساسی داخل کد نیست؛ همه از متغیرهای محیطی (Environment) خوانده می‌شوند:
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
ARROW = "⬅️"  # جهت فلش: از قیمت اصلی به قیمت تخفیف‌خورده (متن راست‌به‌چپ)

BTN_BUY = "🛒 خرید سرویس"
BTN_MY = "📦 سرویس‌های من"
BTN_TARIFFS = "📈 تعرفه سرویس‌ها"
BTN_WALLET = "💳 شارژ کیف پول"
BTN_ACCOUNT = "👤 حساب کاربری"
BTN_SUPPORT = "🎧 پشتیبانی"
MENU_TEXTS = {BTN_BUY, BTN_MY, BTN_TARIFFS, BTN_WALLET, BTN_ACCOUNT, BTN_SUPPORT}

CATEGORY_LABELS = {"economic": "اقتصادی", "vip": "VIP", "gaming": "گیمینگ"}
MAX_DISCOUNT = 90


# ---------------------------------------------------------------------------
# مدل‌های دیتابیس
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


class Rating(Base):
    __tablename__ = "ratings"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    request_id = Column(Integer)
    score = Column(Integer)
    created_at = Column(String)


class ReceiptHash(Base):
    __tablename__ = "receipt_hashes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    sha256 = Column(String)
    unique_id = Column(String)
    user_id = Column(BigInteger)
    request_id = Column(Integer)
    created_at = Column(String)


class ScheduledBroadcast(Base):
    __tablename__ = "scheduled_broadcasts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    segment = Column(String, default="all")
    chat_id = Column(BigInteger)  # چتی که پیام اصلی در آن است (ادمین)
    message_id = Column(BigInteger)
    run_at = Column(String)  # YYYY-MM-DDTHH:MM به وقت ایران
    status = Column(String, default="pending")  # pending | sending | sent | cancelled
    created_by = Column(BigInteger)
    created_at = Column(String)
    result = Column(String)


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
    "🔹 <b>چطور وصل شوم؟</b>\nساب‌لینک را در برنامه V2Box اضافه کنید (آموزش داخل کانال).\n\n"
    "🔹 <b>سرویس‌ها چند روزه‌اند؟</b>\nهر سرویس ۳۰ روزه است و قبل از پایان یادآوری می‌گیرید.\n\n"
    "🔹 <b>چطور تمدید کنم؟</b>\nاز بخش «سرویس‌های من» روی سرویس بزنید و دکمه تمدید را انتخاب کنید."
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
    "loyalty_bronze_percent": "5",
    "loyalty_silver_percent": "10",
    "loyalty_gold_percent": "15",
    "rating_on": "1",
    "rating_low_max": "2",
    "monitor_on": "0",
    "monitor_urls": "",
    "monitor_fails": "3",
    "monitor_notify_users": "1",
    "outage_text": "⚠️ در حال حاضر یکی از سرورهای ما مشکل دارد و تیم فنی در حال رفع آن است. به‌محض برطرف شدن اطلاع می‌دهیم. از صبر شما سپاسگزاریم 🙏",
    "recovery_text": "✅ مشکل سرور برطرف شد و سرویس‌ها به حالت عادی برگشتند. ممنون از صبوری شما 🙏",
    "post_on": "0",
    "post_channel": "",
    "post_times": "10:00,20:00",
    "post_title": "",
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

# متادیتای تنظیماتی که از منوی «تنظیمات ربات» ویرایش می‌شوند
SETTINGS = {
    "discount_percent": dict(page="disc", type="num", label="تخفیف عمومی (٪)", min=0, max=MAX_DISCOUNT),
    "first_on": dict(page="disc", type="toggle", label="تخفیف خرید اول"),
    "first_percent": dict(page="disc", type="num", label="درصد تخفیف خرید اول", min=0, max=50),
    "loyalty_on": dict(page="disc", type="toggle", label="تخفیف وفاداری"),
    "loyalty_bronze_percent": dict(page="disc", type="num", label="🥉 برنزی (۲ خرید در ماه) ٪", min=0, max=50),
    "loyalty_silver_percent": dict(page="disc", type="num", label="🥈 نقره‌ای (۴ خرید در ماه) ٪", min=0, max=50),
    "loyalty_gold_percent": dict(page="disc", type="num", label="🥇 طلایی (۶ خرید در ماه) ٪", min=0, max=50),
    "rating_on": dict(page="auto", type="toggle", label="نظرسنجی بعد از تحویل"),
    "rating_low_max": dict(page="auto", type="num", label="ارسال به ادمین اگر امتیاز ≤", min=1, max=4),
    "monitor_on": dict(page="mon", type="toggle", label="مانیتور خودکار سرور"),
    "monitor_urls": dict(page="mon", type="text", label="آدرس‌های مانیتور (http/https، با کاما جدا کنید)"),
    "monitor_fails": dict(page="mon", type="num", label="تعداد خطای پشت‌سرهم تا اعلان قطعی", min=1, max=20),
    "monitor_notify_users": dict(page="mon", type="toggle", label="اعلان قطعی/وصلی برای همه کاربران"),
    "outage_text": dict(page="mon", type="text", label="متن اعلان قطعی"),
    "recovery_text": dict(page="mon", type="text", label="متن اعلان وصل شدن"),
    "post_on": dict(page="chpost", type="toggle", label="پست خودکار تعرفه در کانال"),
    "post_channel": dict(page="chpost", type="text", label="کانال (@username یا آیدی عددی -100...)"),
    "post_times": dict(page="chpost", type="text", label="ساعت‌های پست (مثلاً 10:00,20:00)"),
    "post_title": dict(page="chpost", type="text", label="عنوان پست (اختیاری)"),
    "card_number": dict(page="pay", type="text", label="شماره کارت"),
    "card_holder": dict(page="pay", type="text", label="نام صاحب کارت"),
    "charge_amounts": dict(page="pay", type="text", label="مبالغ شارژ (با کاما جدا کنید)"),
    "min_charge": dict(page="pay", type="num", label="حداقل مبلغ شارژ (تومان)", min=0, max=100000000),
    "maintenance_on": dict(page="maint", type="toggle", label="حالت تعمیر"),
    "maintenance_text": dict(page="maint", type="text", label="پیام حالت تعمیر"),
    "welcome_text": dict(page="start", type="text", label="متن خوش‌آمدگویی ({name} = اسم کاربر)"),
    "welcome_banner": dict(page="start", type="photo", label="بنر پیام شروع"),
    "channel_link": dict(page="start", type="text", label="لینک کانال"),
    "sticker_welcome": dict(page="start", type="sticker", label="استیکر خوش‌آمدگویی"),
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
    "disc": "🏷 تخفیف‌ها",
    "pay": "💳 پرداخت و شارژ",
    "maint": "🔧 حالت تعمیر",
    "start": "👋 پیام شروع و استیکرها",
    "support": "🎧 پشتیبانی و سوالات متداول",
    "ref": "🎁 دعوت دوستان",
    "auto": "🔔 یادآوری، گزارش و نظرسنجی",
    "mon": "📡 مانیتور سرور و اعلان قطعی",
    "chpost": "📣 پست خودکار کانال",
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
# وضعیت‌های FSM
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
    broadcast_time = State()
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
# تخفیف‌ها (جمعی) و قیمت‌گذاری
# ---------------------------------------------------------------------------
# سطح‌های وفاداری بر اساس تعداد خرید در ماه (از بالا به پایین)
LOYALTY_TIERS = [
    (6, "gold", "🥇 طلایی"),
    (4, "silver", "🥈 نقره‌ای"),
    (2, "bronze", "🥉 برنزی"),
]
TIER_RANK = {"bronze": 1, "silver": 2, "gold": 3}


def tier_for_count(count):
    """(کلید, برچسب, درصد) سطح مربوط به تعداد خرید ماه، یا None."""
    for need, key, label in LOYALTY_TIERS:
        if (count or 0) >= need:
            return key, label, get_int(f"loyalty_{key}_percent")
    return None


def discount_breakdown(user_id):
    """(درصد کل, [(درصد, توضیح), ...]) برای کاربر؛ تخفیف‌ها جمع می‌شوند."""
    with SessionLocal() as s:
        user = s.get(User, user_id)
        is_new = bool(user.is_new_user) if user else True
        has_monthly = bool(user.has_monthly_discount) if user else False
        month_count = (user.purchase_count_current_month or 0) if user else 0

    first = get_int("first_percent") if (get_bool("first_on") and is_new) else 0
    tier = tier_for_count(month_count) if (get_bool("loyalty_on") and has_monthly) else None
    loyal = tier[2] if tier else 0
    loyal_label = f"تخفیف وفاداری {tier[1]}" if tier else "تخفیف وفاداری"

    parts = []
    # «خرید اول» یا «وفاداری»؛ اگر هر دو بود، بزرگ‌تر اعمال می‌شود
    if first or loyal:
        if first >= loyal:
            parts.append((first, "تخفیف خرید اول"))
        else:
            parts.append((loyal, loyal_label))
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
        return None, "شما قبلاً از این کد استفاده کرده‌اید."
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


# ---------------------------------------------------------------------------
# توابع کمکی پیام‌رسانی
# ---------------------------------------------------------------------------
async def edit_or_send(call: types.CallbackQuery, text_, markup=None):
    try:
        await call.message.edit_text(text_, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        await call.message.answer(text_, reply_markup=markup)
    except AttributeError:  # پیام قدیمی/غیرقابل‌دسترس
        await call.bot.send_message(chat_id=call.from_user.id, text=text_, reply_markup=markup)


async def reply_to(target, text_, markup=None):
    """پیام را برای Message ارسال یا برای CallbackQuery ویرایش می‌کند."""
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
    """متن یا عکس پیام را با یک سرتیتر برای chat_id می‌فرستد."""
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
                [b(text="🎫 تیکت‌ها", callback_data="admin_tickets")],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                b(text="🟢 پاسخ داده نشده", callback_data="admin_unanswered"),
                b(text="🔴 پاسخ داده شده", callback_data="admin_answered"),
            ],
            [b(text="👥 مدیریت کاربران", callback_data="admin_users"), b(text="🎫 تیکت‌ها", callback_data="admin_tickets")],
            [b(text="📊 آمار کلی فروش", callback_data="admin_sales_stats"), b(text="🧾 لاگ فعالیت‌ها", callback_data="admin_logs")],
            [b(text="📦 مدیریت پلن‌ها", callback_data="admin_plans"), b(text="🏷 کدهای تخفیف", callback_data="admin_codes")],
            [b(text="📢 پیام همگانی", callback_data="admin_broadcast_start"), b(text="🧰 بکاپ", callback_data="admin_backup")],
            [b(text="⚙️ تنظیمات ربات", callback_data="admin_settings")],
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
            [b(text="⚙️ پنل مدیریت", callback_data="admin_home")],
        ]
    )


# ---------------------------------------------------------------------------
# میدل‌ور: ثبت کاربر، مسدودسازی، حالت تعمیر، ریاکشن
# ---------------------------------------------------------------------------
def touch_user(tg_user):
    """کاربر را ثبت/به‌روزرسانی می‌کند. اگر کاربر تازه ساخته شد True برمی‌گرداند."""
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
                    await event.answer(esc(msg))
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
            f"💳 موجودی: {balance:,} تومان\n📦 سرویس‌های فعال: {active}"
        )

    total, parts = discount_breakdown(user_id)
    if total > 0:
        body += "\n\n" + breakdown_text(total, parts)

    for cname, left in expiring[:3]:
        if left >= 0:
            body += f"\n⏰ سرویس «{esc(cname or '')}» {left} روز دیگر تمام می‌شود."
        else:
            body += f"\n⚠️ سرویس «{esc(cname or '')}» منقضی شده است."

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
# تعرفه‌ها
# ---------------------------------------------------------------------------
def render_tariffs(user_id):
    total, parts = discount_breakdown(user_id)
    with SessionLocal() as s:
        rows = [(r.category, r.name, r.price) for r in s.query(Service).order_by(Service.category, Service.id).all()]

    out = "📊 <b>لیست تعرفه و قیمت‌های ویژه شما:</b>\n"
    if total > 0:
        out += "\n" + breakdown_text(total, parts) + "\n"
    current = None
    for cat, name, price in rows:
        if cat != current:
            current = cat
            out += f"\n📂 <b>دسته‌بندی {esc(CATEGORY_LABELS.get(cat, cat))}:</b>\n"
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
    out = "لطفاً دسته‌بندی مورد نظر را انتخاب کنید:"
    if total > 0:
        out += "\n\n" + breakdown_text(total, parts)
    if get_bool("loyalty_on"):
        out += (
            "\n💎 باشگاه وفاداری (خرید در هر ماه): "
            f"۲ خرید 🥉 {get_int('loyalty_bronze_percent')}٪ | "
            f"۴ خرید 🥈 {get_int('loyalty_silver_percent')}٪ | "
            f"۶ خرید 🥇 {get_int('loyalty_gold_percent')}٪"
        )
    return out


def buy_menu_markup():
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="🌱 سرویس اقتصادی", callback_data="cat_economic")],
            [b(text="💎 سرویس وی‌آی‌پی", callback_data="cat_vip")],
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

    out = f"📊 لیست پلن‌های {esc(CATEGORY_LABELS.get(category, category))}:\n"
    if total > 0:
        out += "\n" + breakdown_text(total, parts) + "\n"
    out += "\n"
    keyboard = []
    for sid, name, price in services:
        out += price_line(name, price, price_with_percent(price, total))
        keyboard.append([InlineKeyboardButton(text=f"خرید {name}", callback_data=f"buy_{sid}")])
    if not services:
        out += "فعلاً پلنی در این دسته وجود ندارد."
    keyboard.append([InlineKeyboardButton(text="⬅️ برگشت به منوی خرید", callback_data="main_buy")])
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
        "\nشما می‌توانید از طریق کارت یا موجودی کیف پول پرداخت کنید.\n\n"
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
            [b(text="⬅️ انصراف و بازگشت", callback_data="main_buy")],
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


@dp.message(PaymentState.waiting_for_code, ~F.text.in_(MENU_TEXTS))
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
    await edit_or_send(call, "لطفاً اسکرین‌شات یا عکس رسید واریزی خود را در چت بفرستید تا بررسی شود.")


async def receipt_fingerprint(bot, photo):
    """(file_unique_id, sha256 محتوای عکس) برای تشخیص رسید تکراری."""
    uniq = photo.file_unique_id
    digest = None
    try:
        import hashlib

        buf = io.BytesIO()
        await bot.download(photo.file_id, destination=buf)
        digest = hashlib.sha256(buf.getvalue()).hexdigest()
    except Exception as e:  # noqa: BLE001
        logging.warning("receipt hash failed: %s", e)
    return uniq, digest


def find_duplicate_receipt(session, uniq, digest):
    """(شماره درخواست قبلی, آیدی کاربر قبلی) اگر همین رسید قبلاً ثبت شده و رد نشده باشد."""
    conds = []
    if uniq:
        conds.append(ReceiptHash.unique_id == uniq)
    if digest:
        conds.append(ReceiptHash.sha256 == digest)
    if not conds:
        return None
    for rh in session.query(ReceiptHash).filter(or_(*conds)).all():
        prev = session.get(Request, rh.request_id)
        if prev is not None and prev.status != "rejected":
            return prev.id, prev.user_id
    return None


def save_receipt_fingerprint(session, uniq, digest, user_id, req_id):
    session.add(
        ReceiptHash(
            sha256=digest, unique_id=uniq, user_id=user_id, request_id=req_id, created_at=get_current_jalali()
        )
    )


async def handle_duplicate_receipt(message: types.Message, dup):
    prev_req, prev_user = dup
    await message.answer(
        "⚠️ این رسید قبلاً در سیستم ثبت شده است و نمی‌توان دوباره از آن استفاده کرد.\n"
        "اگر رسید جدیدی دارید همان را بفرستید؛ در صورت اشتباه از بخش پشتیبانی تیکت بزنید."
    )
    await notify_staff(
        message.bot,
        "🚫 <b>تلاش برای ارسال رسید تکراری</b>\n"
        f"👤 کاربر: {user_label(message.from_user)} (<code>{message.from_user.id}</code>)\n"
        f"🔁 رسید قبلاً برای درخواست #{prev_req} (کاربر <code>{prev_user}</code>) ثبت شده بود.",
    )


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
    uniq, digest = await receipt_fingerprint(message.bot, message.photo[-1])
    dup = None
    with SessionLocal() as s:
        get_or_create_user(s, user_id)
        dup = find_duplicate_receipt(s, uniq, digest)
        if dup is None:
            try:
                req = build_request(s, user_id, co, False, photo_id)
            except ValueError as e:
                await message.answer(f"❌ {e}")
                return
            save_receipt_fingerprint(s, uniq, digest, user_id, req.id)
            req_id, req_type = req.id, req.req_type
            s.commit()
    if dup is not None:
        await handle_duplicate_receipt(message, dup)
        return

    await state.clear()
    await message.answer(
        f"رسید شما در تاریخ {get_current_jalali()} دریافت شد و به زودی بررسی می‌شود. با تشکر از خرید شما! 🙏"
    )
    await send_sticker(message.bot, message.chat.id, "sticker_purchase")
    await notify_staff(
        message.bot, request_caption(req_id, message.from_user, co, req_type, False),
        admin_request_markup(req_id), photo=photo_id,
    )


@dp.message(PaymentState.waiting_for_receipt, ~F.text.in_(MENU_TEXTS))
@dp.message(PaymentState.waiting_for_charge_receipt, ~F.text.in_(MENU_TEXTS))
async def receipt_not_photo(message: types.Message):
    await message.answer("لطفاً رسید را به صورت عکس (اسکرین‌شات) ارسال کنید.")


# ---------------------------------------------------------------------------
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
    rows.append([b(text="✍️ مبلغ دلخواه", callback_data="charge_custom")])
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
        call, f"✍️ مبلغ دلخواه را به تومان و به صورت عدد بفرستید (حداقل {get_int('min_charge'):,} تومان):"
    )


@dp.message(PaymentState.waiting_for_custom_amount, ~F.text.in_(MENU_TEXTS))
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
    amount = int(call.data.split("_")[1])
    if amount < get_int("min_charge"):
        await call.answer("این مبلغ کمتر از حداقل شارژ است.", show_alert=True)
        return
    await call.answer()
    await send_charge_instructions(call, state, amount)


@dp.callback_query(F.data == "send_charge_receipt")
async def prompt_charge_receipt_handler(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    if not data.get("charge_amount"):
        await edit_or_send(call, "لطفاً ابتدا مبلغ شارژ را انتخاب کنید.", back_home_markup())
        return
    await state.set_state(PaymentState.waiting_for_charge_receipt)
    await edit_or_send(call, "لطفاً اسکرین‌شات یا عکس رسید واریزی خود را در چت ارسال کنید:")


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
    uniq, digest = await receipt_fingerprint(message.bot, message.photo[-1])
    dup = None
    with SessionLocal() as s:
        get_or_create_user(s, user_id)
        dup = find_duplicate_receipt(s, uniq, digest)
        if dup is None:
            req = Request(
                user_id=user_id, req_type="charge", plan_name="شارژ کیف پول", status="unanswered",
                created_at=get_current_jalali(), receipt_file_id=photo_id, price=amount, paid_wallet=False,
            )
            s.add(req)
            s.flush()
            save_receipt_fingerprint(s, uniq, digest, user_id, req.id)
            req_id = req.id
            s.commit()
    if dup is not None:
        await handle_duplicate_receipt(message, dup)
        return

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
# سرویس‌های من
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
        "📦 <b>سرویس‌های فعال شما (برای مشاهده جزئیات انتخاب کنید):</b>",
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
    rows.append([b(text="⬅️ بازگشت به لیست سرویس‌ها", callback_data="my_services_callback")])
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

    tier = tier_for_count(count) if has_monthly else None
    next_need = next((n for n, _k, _l in reversed(LOYALTY_TIERS) if n > count), None)
    if tier:
        status_text = f"سطح {tier[1]} ({tier[2]}٪ تخفیف)"
        if next_need:
            status_text += f" - تا سطح بعد {next_need - count} خرید دیگر"
    else:
        status_text = f"⏳ {count} خرید در این ماه (برای سطح برنزی ۲ خرید لازم است)"

    out = (
        "👤 <b>حساب کاربری</b>\n\n"
        f"🆔 آیدی عددی: <code>{user_id}</code>\n"
        f"💳 موجودی کیف پول: {balance:,} تومان\n"
        f"🟢 تعداد سرویس‌های فعال: {active_count}\n"
        f"🎁 وضعیت آفر وفاداری: {status_text}"
    )
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="📜 تاریخچه تراکنش‌ها", callback_data="tx_history")],
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
    out = "📜 <b>تاریخچه تراکنش‌ها و درخواست‌های شما:</b>\n\n" + "\n".join(lines)
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="⬅️ برگشت به حساب کاربری", callback_data="back_to_account")],
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
        f"<b>{get_int('referral_reward'):,} تومان</b> به کیف پول شما اضافه می‌شود.\n\n"
        f"🔗 لینک اختصاصی شما:\n<code>{esc(link)}</code>\n\n"
        f"👥 دعوت‌شده‌ها: {invited}\n✅ پاداش گرفته‌شده: {rewarded}"
    )
    await edit_or_send(call, out, back_home_markup())


# ---------------------------------------------------------------------------
# پشتیبانی، سوالات متداول و تیکت (سمت کاربر)
# ---------------------------------------------------------------------------
def support_view():
    support = get_setting("support_info").strip()
    out = "🎧 <b>پشتیبانی Swan VPN</b>\n\n"
    if support:
        out += f"برای ارتباط مستقیم با ادمین به آیدی زیر پیام دهید:\n👉 {esc(support)}\n\n"
    out += "یا از دکمه زیر تیکت بفرستید تا همین‌جا جواب بگیرید."
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
            [b(text="⬅️ بازگشت", callback_data="support_menu")],
        ]
    )
    faq = get_setting("faq_text")
    # متن پیش‌فرض HTML است؛ متن ساخته‌شده توسط ادمین escape می‌شود
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
    await edit_or_send(call, "✍️ پیام یا مشکل خود را بنویسید (می‌توانید عکس هم بفرستید):")


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
    await call.message.answer(f"✍️ پاسخ شما برای تیکت #{tid} را بنویسید:")


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
    await message.answer(f"✅ تیکت #{tid} ثبت شد. پاسخ پشتیبانی همین‌جا برایتان ارسال می‌شود.")

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


# ---------------------------------------------------------------------------
# پنل ادمین: ورود
# ---------------------------------------------------------------------------
@dp.message(Command("admin"), Staff())
async def admin_panel_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "⚙️ <b>پنل مدیریت:</b>", reply_markup=get_admin_main_keyboard(message.from_user.id)
    )


@dp.callback_query(F.data == "admin_home", Staff())
async def admin_home_callback(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    markup = get_admin_main_keyboard(call.from_user.id)
    try:
        await call.message.edit_text("⚙️ <b>پنل مدیریت:</b>", reply_markup=markup)
    except TelegramBadRequest:
        await call.message.answer("⚙️ <b>پنل مدیریت:</b>", reply_markup=markup)


# ---------------------------------------------------------------------------
# پنل ادمین: صف درخواست‌ها
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "admin_unanswered", Staff())
async def admin_unanswered_handler(call: types.CallbackQuery):
    with SessionLocal() as s:
        reqs = [
            (r.id, r.user_id, r.req_type, r.plan_name, r.created_at)
            for r in s.query(Request).filter(Request.status == "unanswered").order_by(Request.id).limit(15).all()
        ]
    if not reqs:
        await call.answer("درخواست پاسخ‌داده‌نشده‌ای وجود ندارد.", show_alert=True)
        return

    await call.answer()
    out = "🔴 <b>درخواست‌های در انتظار پاسخ:</b>\n\n"
    keyboard = []
    for req_id, u_id, rtype, plan, created in reqs:
        out += (
            f"🆔 #{req_id} | {TYPE_LABELS.get(rtype, rtype or '')} | کاربر: {u_id}\n"
            f"📦 {esc(plan or '')} | {created}\n-----------------------------------\n"
        )
        keyboard.append(
            [InlineKeyboardButton(text=f"💬 بررسی درخواست #{req_id}", callback_data=f"admin_detail_{req_id}")]
        )
    keyboard.append([InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="admin_home")])
    await edit_or_send(call, out, InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data == "admin_answered", Staff())
async def admin_answered_handler(call: types.CallbackQuery):
    with SessionLocal() as s:
        reqs = [
            (r.id, r.user_id, r.req_type, r.plan_name, r.created_at, r.answered_at)
            for r in s.query(Request).filter(Request.status != "unanswered").order_by(Request.id.desc()).limit(10).all()
        ]
    if not reqs:
        await call.answer("درخواست پاسخ‌داده‌شده‌ای یافت نشد.", show_alert=True)
        return

    await call.answer()
    out = "🟢 <b>آخرین درخواست‌های بررسی‌شده:</b>\n\n"
    for req_id, u_id, rtype, plan, created, answered in reqs:
        out += (
            f"🆔 #{req_id} | {TYPE_LABELS.get(rtype, rtype or '')} | کاربر: {u_id}\n"
            f"📦 {esc(plan or '')}\n⏱ ثبت: {created}\n✅ بررسی: {answered}\n"
            "-----------------------------------\n"
        )
    buttons = [InlineKeyboardButton(text=f"💬 #{r[0]}", callback_data=f"admin_detail_{r[0]}") for r in reqs]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="admin_home")])
    await edit_or_send(call, out, InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("admin_detail_"), Staff())
async def admin_detail_handler(call: types.CallbackQuery):
    req_id = int(call.data.split("_")[2])
    with SessionLocal() as s:
        r = s.get(Request, req_id)
        if r is None:
            await call.answer("درخواست یافت نشد.", show_alert=True)
            return
        u_id, r_type, plan = r.user_id, r.req_type, r.plan_name
        status, created, answered = r.status, r.created_at, r.answered_at
        receipt_id, price, code = r.receipt_file_id, r.price or 0, r.discount_code

    await call.answer()
    status_text = {"answered": "پاسخ داده شده 🟢", "rejected": "رد شده ❌"}.get(status, "در انتظار بررسی 🔴")
    out = (
        f"📋 <b>جزئیات کامل درخواست #{req_id}</b>\n\n"
        f"👤 آیدی کاربر: <code>{u_id}</code>\n"
        f"📦 نوع: {TYPE_LABELS.get(r_type, r_type or '')} - {esc(plan or '')}\n"
        f"💰 مبلغ: {price:,} تومان\n"
    )
    if code:
        out += f"🏷 کد تخفیف: {esc(code)}\n"
    out += f"📌 وضعیت: {status_text}\n⏱ زمان ثبت: {created}\n"
    if answered:
        out += f"✅ زمان بررسی: {answered}\n"

    b = InlineKeyboardButton
    back_cb = "admin_unanswered" if status == "unanswered" else "admin_answered"
    keyboard = [
        [b(text="⬅️ بازگشت به لیست", callback_data=back_cb)],
        [b(text="🏠 پنل اصلی", callback_data="admin_home")],
    ]
    if status == "unanswered":
        keyboard.insert(
            0,
            [
                b(text="✅ تایید", callback_data=f"admin_confirm_{req_id}"),
                b(text="❌ رد درخواست", callback_data=f"admin_reject_{req_id}"),
            ],
        )
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    try:
        if receipt_id and receipt_id != "None":
            try:
                await call.message.delete()
            except Exception:  # noqa: BLE001
                pass
            await call.bot.send_photo(chat_id=call.message.chat.id, photo=receipt_id, caption=out, reply_markup=markup)
        else:
            await edit_or_send(call, out, markup)
    except Exception as e:  # noqa: BLE001
        logging.error("Error showing request details: %s", e)


def register_purchase(s, user_id):
    """شمارنده خرید/وفاداری/دعوت را به‌روز می‌کند. (پیام وفاداری؟, (آیدی معرف, مبلغ) | None)"""
    user = get_or_create_user(s, user_id)
    current_month = jalali_today().strftime("%Y/%m")
    was_new = bool(user.is_new_user)
    count = user.purchase_count_current_month or 0
    same_month = user.last_purchase_month == current_month
    prev_tier = tier_for_count(count) if same_month else None
    new_count = (count + 1) if same_month else 1
    user.is_new_user = False
    user.purchase_count_current_month = new_count
    user.last_purchase_month = current_month
    user.has_monthly_discount = new_count >= 2
    new_tier = tier_for_count(new_count)
    monthly_notif = None
    if new_tier and get_bool("loyalty_on"):
        if prev_tier is None or TIER_RANK[new_tier[0]] > TIER_RANK[prev_tier[0]]:
            monthly_notif = new_tier

    reward = None
    if was_new and user.referred_by and not user.referral_rewarded and get_bool("referral_on"):
        amount = get_int("referral_reward")
        ref = s.get(User, user.referred_by)
        if ref is not None and amount > 0:
            ref.balance = (ref.balance or 0) + amount
            user.referral_rewarded = True
            reward = (ref.user_id, amount)
    return monthly_notif, reward


async def post_purchase_notifications(bot, user_id, monthly_notif, reward):
    if monthly_notif:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"🔥 <b>تبریک! به سطح {monthly_notif[1]} باشگاه وفاداری رسیدید و "
                f"{monthly_notif[2]}٪ تخفیف وفاداری برای خریدهای بعدی شما فعال شد.</b>",
            )
        except Exception:  # noqa: BLE001
            pass
    if reward:
        try:
            await bot.send_message(
                chat_id=reward[0],
                text=f"🎉 دوست دعوت‌شده شما اولین خریدش را انجام داد و {reward[1]:,} تومان به کیف پول شما اضافه شد!",
            )
        except Exception:  # noqa: BLE001
            pass


async def send_rating_request(bot, user_id, req_id):
    """نظرسنجی ۱ تا ۵ بعد از تحویل سرویس."""
    if not get_bool("rating_on"):
        return
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[b(text=f"{n} ⭐", callback_data=f"rate_{req_id}_{n}") for n in range(1, 6)]]
    )
    try:
        await bot.send_message(
            chat_id=user_id,
            text="⭐ <b>نظر شما برای ما مهم است!</b>\nاز ۱ تا ۵ به تجربه خرید و تحویل سرویس امتیاز بدهید:",
            reply_markup=markup,
        )
    except Exception as e:  # noqa: BLE001
        logging.info("rating request to %s failed: %s", user_id, e)


@dp.callback_query(F.data.regexp(r"^rate_\d+_[1-5]$"))
async def rating_handler(call: types.CallbackQuery):
    _, req_id_s, score_s = call.data.split("_")
    req_id, score = int(req_id_s), int(score_s)
    uid = call.from_user.id
    with SessionLocal() as s:
        r = s.get(Request, req_id)
        if r is None or r.user_id != uid:
            await call.answer("سفارش یافت نشد.", show_alert=True)
            return
        if s.query(Rating).filter(Rating.request_id == req_id).first():
            await call.answer("امتیاز این سفارش قبلاً ثبت شده است.", show_alert=True)
            return
        s.add(Rating(user_id=uid, request_id=req_id, score=score, created_at=get_current_jalali()))
        plan = r.plan_name
        s.commit()

    await call.answer()
    low = score <= get_int("rating_low_max")
    if low:
        b = InlineKeyboardButton
        markup = InlineKeyboardMarkup(inline_keyboard=[[b(text="🎫 ارسال تیکت", callback_data="ticket_new")]])
        await edit_or_send(
            call,
            f"🙏 ممنون از امتیاز شما ({score} ⭐). متأسفیم که تجربه خوبی نداشتید؛ "
            "اگر مشکلی هست از طریق تیکت بگویید تا سریع بررسی کنیم.",
            markup,
        )
        text_ = (
            f"⚠️ <b>امتیاز پایین: {score}/5</b>\n"
            f"👤 کاربر: {user_label(call.from_user)} (<code>{uid}</code>)\n"
            f"📦 سفارش #{req_id}: {esc(plan or '')}\n"
            f"⏱ {get_current_jalali()}"
        )
        for cid in staff_ids():
            markup_admin = None
            if is_main(cid):
                markup_admin = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="✉️ پیام به کاربر", callback_data=f"pm_{uid}"),
                            InlineKeyboardButton(text="👤 مدیریت کاربر", callback_data=f"manage_user_{uid}"),
                        ]
                    ]
                )
            try:
                await call.bot.send_message(chat_id=cid, text=text_, reply_markup=markup_admin)
            except Exception as e:  # noqa: BLE001
                logging.warning("low rating notify failed for %s: %s", cid, e)
    else:
        await edit_or_send(call, f"🙏 ممنون از امتیاز شما ({score} ⭐)!")


@dp.callback_query(F.data.startswith("admin_confirm_"), Staff())
async def admin_confirm_action(call: types.CallbackQuery, state: FSMContext):
    req_id = int(call.data.split("_")[2])
    result = None  # برای شارژ و تمدید
    with SessionLocal() as s:
        r = s.get(Request, req_id)
        if r is None:
            await call.answer("درخواست یافت نشد.", show_alert=True)
            return
        if r.status != "unanswered":
            await call.answer("این درخواست قبلاً بررسی شده است.", show_alert=True)
            return
        rtype, uid, plan, price = r.req_type, r.user_id, r.plan_name, r.price or 0

        if rtype == "charge":
            user = get_or_create_user(s, uid)
            user.balance = (user.balance or 0) + price
            r.status = "answered"
            r.answered_at = get_current_jalali()
            s.commit()
            result = ("charge", uid, price, None, None)

        elif rtype == "renew":
            cfg = s.get(UserConfig, r.config_id) if r.config_id else None
            if cfg is None:
                await call.answer("سرویس مربوط به این تمدید پیدا نشد. درخواست را رد کنید.", show_alert=True)
                return
            try:
                current = date.fromisoformat(cfg.expires_at) if cfg.expires_at else today_iran()
            except ValueError:
                current = today_iran()
            new_exp = max(current, today_iran()) + timedelta(days=30)
            cfg.expires_at = new_exp.isoformat()
            cfg.reminded = False
            r.status = "answered"
            r.answered_at = get_current_jalali()
            monthly_notif, reward = register_purchase(s, uid)
            s.commit()
            result = ("renew", uid, plan, new_exp.isoformat(), (monthly_notif, reward))
        else:
            sid = r.service_id

    await call.answer()
    markup = get_admin_main_keyboard(call.from_user.id)

    if result is None:  # خرید جدید: متن کانفیگ را از ادمین می‌گیریم
        await state.update_data(target_user_id=uid, plan_name=plan, req_id=req_id, service_id=sid)
        await state.set_state(PaymentState.waiting_for_admin_config)
        await append_status(
            call, "✅ درخواست تایید شد. لطفاً متن کانفیگ یا ساب‌لینک را برای ارسال به کاربر وارد کنید:", markup
        )
        return

    kind, uid, a, b_, extra = result
    log_action(call.from_user.id, f"تایید درخواست #{req_id} ({kind})")
    if kind == "charge":
        await append_status(call, "✅ وضعیت: واریز تایید شد و حساب کاربر شارژ گردید.", markup)
        try:
            await call.bot.send_message(
                chat_id=uid, text=f"🎉 تبریک!\n\nمبلغ {a:,} تومان با موفقیت به موجودی کیف پول شما اضافه شد."
            )
            await send_sticker(call.bot, uid, "sticker_charge")
        except Exception as e:  # noqa: BLE001
            logging.error("notify charge failed: %s", e)
    else:
        await append_status(call, "✅ وضعیت: تمدید انجام شد.", markup)
        try:
            await call.bot.send_message(
                chat_id=uid,
                text=f"✅ سرویس <b>{esc(a or '')}</b> با موفقیت تمدید شد.\n📅 تاریخ پایان جدید: {jalali_from_iso(b_)}",
            )
            await send_sticker(call.bot, uid, "sticker_config")
        except Exception as e:  # noqa: BLE001
            logging.error("notify renew failed: %s", e)
        await post_purchase_notifications(call.bot, uid, extra[0], extra[1])
        await send_rating_request(call.bot, uid, req_id)


@dp.callback_query(F.data.startswith("admin_reject_"), Staff())
async def admin_reject_action(call: types.CallbackQuery):
    req_id = int(call.data.split("_")[2])
    refunded = 0
    with SessionLocal() as s:
        r = s.get(Request, req_id)
        if r is None:
            await call.answer("درخواست یافت نشد.", show_alert=True)
            return
        if r.status != "unanswered":
            await call.answer("این درخواست قبلاً بررسی شده است.", show_alert=True)
            return
        r.status = "rejected"
        r.answered_at = get_current_jalali()
        target_user = r.user_id
        if r.paid_wallet and (r.price or 0) > 0:
            user = get_or_create_user(s, target_user)
            user.balance = (user.balance or 0) + r.price
            refunded = r.price
        release_code(s, r.discount_code, target_user)
        s.commit()

    await call.answer("درخواست رد شد.")
    log_action(call.from_user.id, f"رد درخواست #{req_id}")
    await append_status(call, "❌ وضعیت: درخواست رد شد.", get_admin_main_keyboard(call.from_user.id))
    msg = "⚠️ متأسفانه پرداخت/درخواست شما توسط ادمین تایید نشد."
    if refunded:
        msg += f"\n💰 مبلغ {refunded:,} تومان به کیف پول شما برگشت داده شد."
    try:
        await call.bot.send_message(chat_id=target_user, text=msg)
    except Exception:  # noqa: BLE001
        pass


@dp.message(PaymentState.waiting_for_admin_config, Staff())
async def process_admin_config(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("لطفاً کانفیگ را به صورت متن ارسال کنید.")
        return

    data = await state.get_data()
    req_id = data.get("req_id")
    target_user_id = data.get("target_user_id")
    plan_name = data.get("plan_name")
    service_id = data.get("service_id")

    start_date = today_iran()
    end_date = start_date + timedelta(days=30)

    with SessionLocal() as s:
        r = s.get(Request, req_id) if req_id else None
        if r is None or r.status != "unanswered":
            await state.clear()
            await message.answer("این درخواست قبلاً بررسی شده است.")
            return
        r.status = "answered"
        r.answered_at = get_current_jalali()
        s.add(
            UserConfig(
                user_id=target_user_id, config_name=plan_name, config_details=message.text,
                service_id=service_id, expires_at=end_date.isoformat(), reminded=False,
            )
        )
        monthly_notif, reward = register_purchase(s, target_user_id)
        s.commit()

    await state.clear()
    log_action(message.from_user.id, f"ارسال کانفیگ برای درخواست #{req_id}")
    await message.answer(
        f"✅ کانفیگ با موفقیت برای کاربر {target_user_id} ارسال و ثبت شد.",
        reply_markup=get_admin_main_keyboard(message.from_user.id),
    )

    try:
        notify = (
            "🎉 <b>سفارش شما تایید و آماده شد!</b>\n\n"
            f"📦 سرویس: {esc(plan_name or '')}\n"
            f"📅 تاریخ شروع: {jalali_from_iso(start_date.isoformat())}\n"
            f"📅 تاریخ پایان: {jalali_from_iso(end_date.isoformat())} (۳۰ روزه)\n\n"
            f"📋 جزئیات/کانفیگ:\n\n<code>{esc(message.text)}</code>\n\n"
            "لطفاً ساب‌لینک خود را طبق آموزش داخل کانال وارد برنامه V2Box کنید.\n"
            "با تشکر از خرید شما. 🙏"
        )
        await message.bot.send_message(chat_id=target_user_id, text=notify)
        await send_sticker(message.bot, target_user_id, "sticker_config")
    except Exception as e:  # noqa: BLE001
        logging.error("Error notifying user about config delivery: %s", e)
    await post_purchase_notifications(message.bot, target_user_id, monthly_notif, reward)
    await send_rating_request(message.bot, target_user_id, req_id)


# ---------------------------------------------------------------------------
# پنل ادمین: مدیریت کاربران
# ---------------------------------------------------------------------------
USERS_PER_PAGE = 10


def users_filter_row(current):
    b = InlineKeyboardButton
    names = [("all", "همه"), ("banned", "🛑 مسدودها"), ("svc", "📦 دارای سرویس")]
    return [
        b(text=("• " if k == current else "") + label, callback_data=f"ausers_{k}_1")
        for k, label in names
    ]


@dp.callback_query(F.data == "admin_users", MainAdmin())
@dp.callback_query(F.data.startswith("ausers_"), MainAdmin())
async def admin_users_list_handler(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    flt, page = "all", 1
    if call.data.startswith("ausers_"):
        _, flt, p = call.data.split("_")
        page = max(1, int(p))
    offset = (page - 1) * USERS_PER_PAGE

    with SessionLocal() as s:
        q = s.query(User)
        if flt == "banned":
            q = q.filter(User.is_banned.is_(True))
        elif flt == "svc":
            q = q.filter(User.user_id.in_(select(UserConfig.user_id)))
        total = q.count()
        users = q.order_by(User.user_id).offset(offset).limit(USERS_PER_PAGE).all()
        rows = [(u.user_id, user_display(u), u.balance or 0, bool(u.is_banned)) for u in users]

    out = f"👥 <b>کاربران ({total} نفر) - صفحه {page}:</b>\n\n"
    keyboard = [users_filter_row(flt)]
    for u_id, name, bal, banned in rows:
        icon = "🛑" if banned else "🟢"
        out += f"{icon} <code>{u_id}</code> | {esc(name)} | {bal:,}\n"
        keyboard.append(
            [InlineKeyboardButton(text=f"👤 {name[:28]}", callback_data=f"manage_user_{u_id}")]
        )
    if not rows:
        out += "کاربری یافت نشد."

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"ausers_{flt}_{page - 1}"))
    if offset + USERS_PER_PAGE < total:
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"ausers_{flt}_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔎 جستجوی کاربر", callback_data="admin_user_search")])
    keyboard.append([InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="admin_home")])
    await edit_or_send(call, out, InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data == "admin_user_search", MainAdmin())
async def admin_user_search_start(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(PaymentState.searching_user)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="انصراف", callback_data="admin_users")]]
    )
    await edit_or_send(call, "🔎 آیدی عددی، یوزرنیم یا بخشی از اسم کاربر را بفرستید:", markup)


@dp.message(PaymentState.searching_user, MainAdmin())
async def admin_user_search_result(message: types.Message, state: FSMContext):
    q = (message.text or "").strip().lstrip("@")
    if not q:
        await message.answer("متن جستجو را بفرستید.")
        return
    with SessionLocal() as s:
        query = s.query(User)
        if q.isdigit():
            query = query.filter(User.user_id == int(q))
        else:
            like = f"%{q}%"
            query = query.filter(or_(User.username.ilike(like), User.first_name.ilike(like)))
        rows = [(u.user_id, user_display(u), u.balance or 0) for u in query.limit(10).all()]
    await state.clear()

    keyboard = []
    out = f"🔎 نتیجه جستجو برای «{esc(q)}»:\n\n"
    for u_id, name, bal in rows:
        out += f"<code>{u_id}</code> | {esc(name)} | {bal:,}\n"
        keyboard.append([InlineKeyboardButton(text=f"👤 {name[:28]}", callback_data=f"manage_user_{u_id}")])
    if not rows:
        out += "کاربری پیدا نشد."
    keyboard.append([InlineKeyboardButton(text="🔙 لیست کاربران", callback_data="admin_users")])
    await message.answer(out, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


async def _render_user_management(target, user_id: int):
    with SessionLocal() as s:
        user = s.get(User, user_id)
        if user is None:
            await reply_to(target, "کاربر یافت نشد.", None)
            return
        name = user_display(user)
        balance = user.balance or 0
        banned = bool(user.is_banned)
        count = user.purchase_count_current_month or 0
        has_monthly = bool(user.has_monthly_discount)
        note = user.note or ""
        referred_by = user.referred_by
        configs = [
            (c.id, c.config_name, c.expires_at)
            for c in s.query(UserConfig).filter(UserConfig.user_id == user_id).all()
        ]

    status_text = "✅ فعال" if has_monthly else f"⏳ {count} خرید این ماه"
    out = (
        f"⚙️ <b>مدیریت کاربر <code>{user_id}</code></b>\n"
        f"👤 {esc(name)}\n\n"
        f"💳 موجودی فعلی: {balance:,} تومان\n"
        f"📋 وضعیت دسترسی: {'🔴 مسدود (بن)' if banned else '🟢 باز'}\n"
        f"🎁 آفر وفاداری: {status_text}\n"
    )
    if referred_by:
        out += f"🔗 دعوت‌شده توسط: <code>{referred_by}</code>\n"
    if note:
        out += f"📝 یادداشت: {esc(note)}\n"
    out += "\n📦 <b>کانفیگ‌های فعال کاربر:</b>\n"

    keyboard = []
    for cfg_id, cfg_name, exp in configs:
        exp_txt = f" - تا {jalali_from_iso(exp)}" if exp else ""
        out += f"🔹 {esc(cfg_name or '')}{exp_txt} (ID: {cfg_id})\n"
        keyboard.append(
            [InlineKeyboardButton(text=f"🗑 حذف کانفیگ #{cfg_id}", callback_data=f"del_cfg_user_{cfg_id}_{user_id}")]
        )
    if not configs:
        out += "هیچ کانفیگ فعالی ندارد.\n"

    b = InlineKeyboardButton
    keyboard.extend(
        [
            [
                b(text="✉️ پیام خصوصی", callback_data=f"pm_{user_id}"),
                b(text="💵 موجودی", callback_data=f"balmenu_{user_id}"),
            ],
            [
                b(text="📝 یادداشت", callback_data=f"note_{user_id}"),
                b(text="📜 تاریخچه", callback_data=f"uhist_{user_id}"),
            ],
            [
                b(
                    text="🔓 آزاد سازی کاربر" if banned else "🚫 مسدود کردن (بن) کاربر",
                    callback_data=f"toggle_ban_{user_id}",
                )
            ],
            [b(text="🔙 بازگشت به لیست کاربران", callback_data="admin_users")],
        ]
    )
    await reply_to(target, out, InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data.startswith("manage_user_"), MainAdmin())
async def admin_user_management_handler(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await _render_user_management(call, int(call.data.split("_")[2]))


@dp.callback_query(F.data.startswith("toggle_ban_"), MainAdmin())
async def toggle_ban_action(call: types.CallbackQuery):
    user_id = int(call.data.split("_")[2])
    with SessionLocal() as s:
        user = get_or_create_user(s, user_id)
        user.is_banned = not bool(user.is_banned)
        new_state = user.is_banned
        s.commit()
    log_action(call.from_user.id, f"{'بن' if new_state else 'آزادسازی'} کاربر {user_id}")
    await call.answer("وضعیت کاربر تغییر یافت.", show_alert=True)
    await _render_user_management(call, user_id)


@dp.callback_query(F.data.startswith("del_cfg_user_"), MainAdmin())
async def delete_config_user_action(call: types.CallbackQuery):
    parts = call.data.split("_")
    cfg_id, target_user_id = int(parts[3]), int(parts[4])
    with SessionLocal() as s:
        cfg = s.get(UserConfig, cfg_id)
        if cfg is not None:
            s.delete(cfg)
            s.commit()
    log_action(call.from_user.id, f"حذف کانفیگ {cfg_id} کاربر {target_user_id}")
    await call.answer("کانفیگ کاربر حذف شد.", show_alert=True)
    try:
        await call.bot.send_message(chat_id=target_user_id, text="⚠️ یکی از سرویس‌های شما توسط ادمین حذف شد.")
    except Exception:  # noqa: BLE001
        pass
    await _render_user_management(call, target_user_id)


@dp.callback_query(F.data.startswith("uhist_"), MainAdmin())
async def admin_user_history(call: types.CallbackQuery):
    uid = int(call.data.split("_")[1])
    lines = render_history(uid, 15)
    if not lines:
        await call.answer("این کاربر تراکنشی ندارد.", show_alert=True)
        return
    await call.answer()
    out = f"📜 <b>تاریخچه کاربر <code>{uid}</code>:</b>\n\n" + "\n".join(lines)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"manage_user_{uid}")]]
    )
    await edit_or_send(call, out, markup)


# --- موجودی ---
@dp.callback_query(F.data.startswith("balmenu_"), MainAdmin())
async def balance_menu(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    uid = int(call.data.split("_")[1])
    with SessionLocal() as s:
        user = s.get(User, uid)
        balance = (user.balance if user else 0) or 0
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="➕ افزایش", callback_data=f"baladd_{uid}"), b(text="➖ کاهش", callback_data=f"balsub_{uid}")],
            [b(text="🎯 تنظیم مبلغ دقیق", callback_data=f"balset_{uid}")],
            [b(text="🔙 بازگشت", callback_data=f"manage_user_{uid}")],
        ]
    )
    await edit_or_send(call, f"💵 موجودی فعلی کاربر <code>{uid}</code>: {balance:,} تومان\nچه کاری انجام شود؟", markup)


@dp.callback_query(F.data.regexp(r"^bal(add|sub|set)_\d+$"), MainAdmin())
async def balance_mode(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    mode, uid = call.data[:6], int(call.data.split("_")[1])
    mode = mode.rstrip("_")
    await state.set_state(PaymentState.adjusting_balance)
    await state.update_data(target_adjust_user=uid, mode=mode)
    label = {"baladd": "افزایش", "balsub": "کاهش", "balset": "تنظیم دقیق"}[mode]
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="انصراف", callback_data=f"manage_user_{uid}")]]
    )
    await edit_or_send(call, f"💵 مبلغ ({label}) را برای کاربر <code>{uid}</code> به عدد وارد کنید:", markup)


@dp.message(PaymentState.adjusting_balance, MainAdmin())
async def process_balance_adjustment(message: types.Message, state: FSMContext):
    data = await state.get_data()
    uid, mode = data.get("target_adjust_user"), data.get("mode")
    raw = (message.text or "").replace(",", "").strip()
    if not uid or not raw.isdigit():
        await message.answer("مقدار وارد شده نامعتبر است. لطفاً فقط عدد وارد کنید.")
        return
    amount = int(raw)

    with SessionLocal() as s:
        user = get_or_create_user(s, uid)
        current = user.balance or 0
        if mode == "baladd":
            new_balance = current + amount
        elif mode == "balsub":
            if amount > current:
                await message.answer(f"مبلغ بیشتر از موجودی فعلی ({current:,}) است.")
                return
            new_balance = current - amount
        else:
            new_balance = amount
        user.balance = new_balance
        s.commit()

    await state.clear()
    log_action(message.from_user.id, f"تغییر موجودی کاربر {uid}: {current:,} -> {new_balance:,}")
    await message.answer(
        f"✅ موجودی کاربر <code>{uid}</code> از {current:,} به {new_balance:,} تغییر یافت.",
        reply_markup=get_admin_main_keyboard(message.from_user.id),
    )
    try:
        await message.bot.send_message(
            chat_id=uid, text=f"💰 موجودی کیف پول شما توسط پشتیبانی تغییر کرد.\nموجودی فعلی: {new_balance:,} تومان"
        )
    except Exception:  # noqa: BLE001
        pass


# --- پیام خصوصی و یادداشت ---
@dp.callback_query(F.data.startswith("pm_"), MainAdmin())
async def private_message_start(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    uid = int(call.data.split("_")[1])
    await state.set_state(PaymentState.private_message)
    await state.update_data(pm_target=uid)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="انصراف", callback_data=f"manage_user_{uid}")]]
    )
    await edit_or_send(call, f"✉️ پیامی که می‌خواهید از طرف ربات برای کاربر <code>{uid}</code> ارسال شود را بفرستید:", markup)


@dp.message(PaymentState.private_message, MainAdmin())
async def private_message_send(message: types.Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get("pm_target")
    await state.clear()
    try:
        await message.copy_to(chat_id=uid)
        await message.answer("✅ پیام ارسال شد.", reply_markup=get_admin_main_keyboard(message.from_user.id))
        log_action(message.from_user.id, f"پیام خصوصی به کاربر {uid}")
    except Exception as e:  # noqa: BLE001
        await message.answer(f"❌ ارسال ناموفق بود: {esc(str(e))}")


@dp.callback_query(F.data.startswith("note_"), MainAdmin())
async def note_start(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    uid = int(call.data.split("_")[1])
    await state.set_state(PaymentState.editing_note)
    await state.update_data(note_target=uid)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="انصراف", callback_data=f"manage_user_{uid}")]]
    )
    await edit_or_send(call, "📝 یادداشت خصوصی را بفرستید (فقط ادمین می‌بیند). برای پاک کردن، - بفرستید:", markup)


@dp.message(PaymentState.editing_note, MainAdmin())
async def note_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get("note_target")
    value = (message.text or "").strip()
    with SessionLocal() as s:
        user = get_or_create_user(s, uid)
        user.note = "" if value == "-" else value
        s.commit()
    await state.clear()
    await message.answer("✅ یادداشت ذخیره شد.", reply_markup=get_admin_main_keyboard(message.from_user.id))


# ---------------------------------------------------------------------------
# پنل ادمین: مدیریت پلن‌ها
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "admin_plans", MainAdmin())
async def admin_plans_list(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    with SessionLocal() as s:
        rows = [(r.id, r.category, r.name, r.price) for r in s.query(Service).order_by(Service.category, Service.id).all()]
    out = "📦 <b>مدیریت پلن‌ها</b>\n\nروی پلن بزنید تا ویرایش یا حذف شود:\n"
    keyboard = []
    for sid, cat, name, price in rows:
        keyboard.append(
            [InlineKeyboardButton(
                text=f"{CATEGORY_LABELS.get(cat, cat)} | {name} | {price:,}", callback_data=f"plan_{sid}"
            )]
        )
    keyboard.append([InlineKeyboardButton(text="➕ افزودن پلن", callback_data="plan_add")])
    keyboard.append([InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="admin_home")])
    await edit_or_send(call, out, InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data.regexp(r"^plan_\d+$"), MainAdmin())
async def admin_plan_view(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    sid = int(call.data.split("_")[1])
    with SessionLocal() as s:
        svc = s.get(Service, sid)
        if svc is None:
            await edit_or_send(call, "پلن یافت نشد.")
            return
        cat, name, price = svc.category, svc.name, svc.price
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="💰 تغییر قیمت", callback_data=f"planprice_{sid}"), b(text="✏️ تغییر نام", callback_data=f"planname_{sid}")],
            [b(text="🗑 حذف پلن", callback_data=f"plandel_{sid}")],
            [b(text="🔙 بازگشت", callback_data="admin_plans")],
        ]
    )
    await edit_or_send(
        call,
        f"📦 <b>{esc(name)}</b>\nدسته: {esc(CATEGORY_LABELS.get(cat, cat))}\nقیمت: {price:,} تومان",
        markup,
    )


@dp.callback_query(F.data.startswith("planprice_"), MainAdmin())
async def plan_price_start(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    sid = int(call.data.split("_")[1])
    await state.set_state(PaymentState.editing_plan_price)
    await state.update_data(plan_id=sid)
    await edit_or_send(call, "قیمت جدید را به تومان و به عدد بفرستید:")


@dp.message(PaymentState.editing_plan_price, MainAdmin())
async def plan_price_save(message: types.Message, state: FSMContext):
    raw = (message.text or "").replace(",", "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("عدد معتبر بفرستید.")
        return
    data = await state.get_data()
    with SessionLocal() as s:
        svc = s.get(Service, data.get("plan_id"))
        if svc is None:
            await state.clear()
            await message.answer("پلن یافت نشد.")
            return
        old = svc.price
        svc.price = int(raw)
        name = svc.name
        s.commit()
    await state.clear()
    log_action(message.from_user.id, f"قیمت پلن {name}: {old:,} -> {int(raw):,}")
    await message.answer("✅ قیمت به‌روز شد.", reply_markup=get_admin_main_keyboard(message.from_user.id))


@dp.callback_query(F.data.startswith("planname_"), MainAdmin())
async def plan_name_start(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    sid = int(call.data.split("_")[1])
    await state.set_state(PaymentState.editing_plan_name)
    await state.update_data(plan_id=sid)
    await edit_or_send(call, "نام جدید پلن را بفرستید:")


@dp.message(PaymentState.editing_plan_name, MainAdmin())
async def plan_name_save(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name or len(name) > 60:
        await message.answer("نام معتبر (حداکثر ۶۰ کاراکتر) بفرستید.")
        return
    data = await state.get_data()
    with SessionLocal() as s:
        svc = s.get(Service, data.get("plan_id"))
        if svc is None:
            await state.clear()
            await message.answer("پلن یافت نشد.")
            return
        svc.name = name
        s.commit()
    await state.clear()
    log_action(message.from_user.id, f"تغییر نام پلن به {name}")
    await message.answer("✅ نام پلن به‌روز شد.", reply_markup=get_admin_main_keyboard(message.from_user.id))


@dp.callback_query(F.data.startswith("plandel_"), MainAdmin())
async def plan_delete_ask(call: types.CallbackQuery):
    await call.answer()
    sid = int(call.data.split("_")[1])
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="✅ بله، حذف شود", callback_data=f"plandelok_{sid}"), b(text="انصراف", callback_data=f"plan_{sid}")]
        ]
    )
    await edit_or_send(call, "⚠️ مطمئنید این پلن حذف شود؟ (کانفیگ‌های قبلی کاربران حفظ می‌شوند)", markup)


@dp.callback_query(F.data.startswith("plandelok_"), MainAdmin())
async def plan_delete_do(call: types.CallbackQuery):
    sid = int(call.data.split("_")[1])
    with SessionLocal() as s:
        svc = s.get(Service, sid)
        name = svc.name if svc else str(sid)
        if svc is not None:
            s.delete(svc)
            s.commit()
    log_action(call.from_user.id, f"حذف پلن {name}")
    await admin_plans_list(call, _NoState())


class _NoState:
    """جایگزین سبک برای فراخوانی داخلی admin_plans_list بدون FSM."""

    async def clear(self):
        return None


@dp.callback_query(F.data == "plan_add", MainAdmin())
async def plan_add_start(call: types.CallbackQuery):
    await call.answer()
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="🌱 اقتصادی", callback_data="planaddcat_economic")],
            [b(text="💎 VIP", callback_data="planaddcat_vip")],
            [b(text="🎮 گیمینگ", callback_data="planaddcat_gaming")],
            [b(text="🔙 بازگشت", callback_data="admin_plans")],
        ]
    )
    await edit_or_send(call, "دسته‌بندی پلن جدید را انتخاب کنید:", markup)


@dp.callback_query(F.data.startswith("planaddcat_"), MainAdmin())
async def plan_add_cat(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    cat = call.data.split("_", 1)[1]
    await state.set_state(PaymentState.adding_plan)
    await state.update_data(category=cat)
    await edit_or_send(
        call, "نام و قیمت پلن را در دو خط بفرستید:\n\n<code>50 گیگ - ۳۰ روزه\n790000</code>"
    )


@dp.message(PaymentState.adding_plan, MainAdmin())
async def plan_add_save(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    name, price = None, None
    if "|" in raw:
        n, _, p = raw.rpartition("|")
        name, price = n.strip(), p.replace(",", "").strip()
    else:
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if len(lines) >= 2:
            name, price = " ".join(lines[:-1]), lines[-1].replace(",", "")
    if not name or not price or not price.isdigit() or int(price) <= 0 or len(name) > 60:
        await message.answer("فرمت نادرست است. نام در یک خط و قیمت (عدد) در خط بعد.")
        return
    data = await state.get_data()
    with SessionLocal() as s:
        s.add(Service(category=data.get("category"), name=name, price=int(price)))
        s.commit()
    await state.clear()
    log_action(message.from_user.id, f"افزودن پلن {name} ({int(price):,})")
    await message.answer("✅ پلن اضافه شد.", reply_markup=get_admin_main_keyboard(message.from_user.id))


# ---------------------------------------------------------------------------
# پنل ادمین: کدهای تخفیف
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "admin_codes", MainAdmin())
async def admin_codes_list(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    with SessionLocal() as s:
        rows = [
            (c.id, c.code, c.percent, c.expires_at, c.max_uses, c.used_count, bool(c.is_active))
            for c in s.query(DiscountCode).order_by(DiscountCode.id.desc()).limit(20).all()
        ]
    out = "🏷 <b>کدهای تخفیف</b>\n\n"
    keyboard = []
    for cid, code, pct, exp, mx, used, active in rows:
        exp_txt = jalali_from_iso(exp) if exp else "بدون انقضا"
        mx_txt = str(mx) if mx else "نامحدود"
        out += f"{'🟢' if active else '⚪️'} <code>{esc(code)}</code> | {pct}٪ | {exp_txt} | {used}/{mx_txt}\n"
        keyboard.append([InlineKeyboardButton(text=f"⚙️ {code}", callback_data=f"code_{cid}")])
    if not rows:
        out += "هنوز کدی ساخته نشده است.\n"
    keyboard.append([InlineKeyboardButton(text="➕ ساخت کد جدید", callback_data="code_add")])
    keyboard.append([InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="admin_home")])
    await edit_or_send(call, out, InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data.regexp(r"^code_\d+$"), MainAdmin())
async def admin_code_view(call: types.CallbackQuery):
    await call.answer()
    cid = int(call.data.split("_")[1])
    with SessionLocal() as s:
        c = s.get(DiscountCode, cid)
        if c is None:
            await edit_or_send(call, "کد یافت نشد.")
            return
        code, active = c.code, bool(c.is_active)
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="🔴 غیرفعال کن" if active else "🟢 فعال کن", callback_data=f"codetoggle_{cid}")],
            [b(text="🗑 حذف کد", callback_data=f"codedel_{cid}")],
            [b(text="🔙 بازگشت", callback_data="admin_codes")],
        ]
    )
    await edit_or_send(call, f"کد <code>{esc(code)}</code> - وضعیت: {'فعال' if active else 'غیرفعال'}", markup)


@dp.callback_query(F.data.startswith("codetoggle_"), MainAdmin())
async def admin_code_toggle(call: types.CallbackQuery):
    cid = int(call.data.split("_")[1])
    with SessionLocal() as s:
        c = s.get(DiscountCode, cid)
        if c is not None:
            c.is_active = not bool(c.is_active)
            s.commit()
    await admin_code_view(call)


@dp.callback_query(F.data.startswith("codedel_"), MainAdmin())
async def admin_code_delete(call: types.CallbackQuery):
    cid = int(call.data.split("_")[1])
    with SessionLocal() as s:
        c = s.get(DiscountCode, cid)
        code = c.code if c else str(cid)
        if c is not None:
            s.query(CodeUse).filter(CodeUse.code_id == cid).delete()
            s.delete(c)
            s.commit()
    log_action(call.from_user.id, f"حذف کد تخفیف {code}")
    await admin_codes_list(call, _NoState())


@dp.callback_query(F.data == "code_add", MainAdmin())
async def admin_code_add_start(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(PaymentState.creating_code)
    await edit_or_send(
        call,
        "کد جدید را به این شکل بفرستید:\n\n<code>کد درصد [تعداد روز اعتبار] [حداکثر استفاده]</code>\n\n"
        "مثال: <code>SWAN20 20 30 100</code>\n(۰ برای روز یا تعداد یعنی بدون محدودیت)",
    )


@dp.message(PaymentState.creating_code, MainAdmin())
async def admin_code_add_save(message: types.Message, state: FSMContext):
    parts = (message.text or "").split()
    if len(parts) < 2 or not re.fullmatch(r"[A-Za-z0-9_\-]{3,20}", parts[0]) or not parts[1].isdigit():
        await message.answer("فرمت نادرست است. مثال: SWAN20 20 30 100")
        return
    code, pct = parts[0].upper(), int(parts[1])
    days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    max_uses = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    if not (1 <= pct <= MAX_DISCOUNT):
        await message.answer(f"درصد باید بین ۱ تا {MAX_DISCOUNT} باشد.")
        return
    with SessionLocal() as s:
        if s.query(DiscountCode).filter(DiscountCode.code == code).first():
            await message.answer("این کد قبلاً ساخته شده است.")
            return
        exp = (today_iran() + timedelta(days=days)).isoformat() if days > 0 else None
        s.add(DiscountCode(code=code, percent=pct, expires_at=exp, max_uses=max_uses, used_count=0, is_active=True))
        s.commit()
    await state.clear()
    log_action(message.from_user.id, f"ساخت کد تخفیف {code} ({pct}٪)")
    await message.answer(f"✅ کد <code>{esc(code)}</code> ساخته شد.", reply_markup=get_admin_main_keyboard(message.from_user.id))


# ---------------------------------------------------------------------------
# پنل ادمین: تیکت‌ها (سمت پشتیبان)
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "admin_tickets", Staff())
async def admin_tickets_list(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    with SessionLocal() as s:
        rows = [
            (t.id, t.user_id, t.created_at)
            for t in s.query(Ticket).filter(Ticket.status == "open").order_by(Ticket.id.desc()).limit(15).all()
        ]
    out = "🎫 <b>تیکت‌های باز:</b>\n\n"
    keyboard = []
    for tid, uid, created in rows:
        out += f"#{tid} | کاربر {uid} | {created}\n"
        keyboard.append([InlineKeyboardButton(text=f"💬 مشاهده تیکت #{tid}", callback_data=f"ticket_view_{tid}")])
    if not rows:
        out += "تیکت بازی وجود ندارد."
    keyboard.append([InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="admin_home")])
    await edit_or_send(call, out, InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data.startswith("ticket_view_"), Staff())
async def admin_ticket_view(call: types.CallbackQuery):
    await call.answer()
    tid = int(call.data.split("_")[2])
    with SessionLocal() as s:
        t = s.get(Ticket, tid)
        if t is None:
            await edit_or_send(call, "تیکت یافت نشد.")
            return
        uid, status = t.user_id, t.status
        msgs = [
            (m.sender, m.text, m.photo_id, m.created_at)
            for m in s.query(TicketMessage).filter(TicketMessage.ticket_id == tid).order_by(TicketMessage.id.desc()).limit(8).all()
        ]
    msgs.reverse()
    out = f"🎫 <b>تیکت #{tid}</b> | کاربر <code>{uid}</code> | {'باز' if status == 'open' else 'بسته'}\n\n"
    for sender, body, photo, created in msgs:
        who = "👤 کاربر" if sender == "user" else "🎧 پشتیبان"
        out += f"{who} ({created}):\n{esc(body or '')}{' 📷' if photo else ''}\n\n"
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="💬 پاسخ", callback_data=f"ticket_reply_{tid}"), b(text="✅ بستن", callback_data=f"ticket_close_{tid}")],
            [b(text="🔙 لیست تیکت‌ها", callback_data="admin_tickets")],
        ]
    )
    await edit_or_send(call, out[:4000], markup)


@dp.callback_query(F.data.startswith("ticket_reply_"), Staff())
async def ticket_staff_reply_start(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    tid = int(call.data.split("_")[2])
    await state.set_state(PaymentState.ticket_staff_reply)
    await state.update_data(ticket_id=tid)
    await call.message.answer(f"✍️ پاسخ خود را برای تیکت #{tid} بنویسید (متن یا عکس):")


@dp.message(PaymentState.ticket_staff_reply, Staff())
async def ticket_staff_reply_send(message: types.Message, state: FSMContext):
    body = message.caption if message.photo else message.text
    if not body and not message.photo:
        await message.answer("لطفاً متن یا عکس بفرستید.")
        return
    data = await state.get_data()
    tid = data.get("ticket_id")
    with SessionLocal() as s:
        t = s.get(Ticket, tid)
        if t is None:
            await state.clear()
            await message.answer("تیکت یافت نشد.")
            return
        uid = t.user_id
        s.add(
            TicketMessage(
                ticket_id=tid, sender="staff", text=body or "",
                photo_id=message.photo[-1].file_id if message.photo else None, created_at=get_current_jalali(),
            )
        )
        s.commit()
    await state.clear()

    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[b(text="↩️ پاسخ به پشتیبانی", callback_data=f"ticket_ureply_{tid}")]]
    )
    try:
        await relay(message.bot, uid, message, f"💬 پاسخ پشتیبانی به تیکت #{tid}:\n\n", markup)
        await message.answer("✅ پاسخ ارسال شد.", reply_markup=get_admin_main_keyboard(message.from_user.id))
        log_action(message.from_user.id, f"پاسخ به تیکت #{tid}")
    except Exception as e:  # noqa: BLE001
        await message.answer(f"❌ ارسال به کاربر ناموفق بود: {esc(str(e))}")


@dp.callback_query(F.data.startswith("ticket_close_"), Staff())
async def ticket_close(call: types.CallbackQuery):
    tid = int(call.data.split("_")[2])
    with SessionLocal() as s:
        t = s.get(Ticket, tid)
        if t is None:
            await call.answer("تیکت یافت نشد.", show_alert=True)
            return
        t.status = "closed"
        uid = t.user_id
        s.commit()
    await call.answer("تیکت بسته شد.")
    log_action(call.from_user.id, f"بستن تیکت #{tid}")
    await append_status(call, f"✅ تیکت #{tid} بسته شد.", get_admin_main_keyboard(call.from_user.id))
    try:
        await call.bot.send_message(chat_id=uid, text=f"✅ تیکت #{tid} شما بسته شد. در صورت نیاز تیکت جدید ثبت کنید.")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# پنل ادمین: آمار، گزارش روزانه، لاگ
# ---------------------------------------------------------------------------
def stats_numbers():
    today = jalali_today().strftime("%Y/%m/%d")
    month = jalali_today().strftime("%Y/%m")
    sale_types = ["buy", "renew"]
    with SessionLocal() as s:
        def revenue(*extra):
            q = s.query(func.coalesce(func.sum(Request.price), 0)).filter(
                Request.status == "answered", Request.req_type.in_(sale_types), *extra
            )
            return int(q.scalar() or 0)

        def sales_count(*extra):
            q = s.query(func.count(Request.id)).filter(
                Request.status == "answered", Request.req_type.in_(sale_types), *extra
            )
            return int(q.scalar() or 0)

        charged_today = int(
            s.query(func.coalesce(func.sum(Request.price), 0))
            .filter(Request.status == "answered", Request.req_type == "charge", Request.answered_at.like(f"{today}%"))
            .scalar() or 0
        )
        return dict(
            today=today, month=month,
            total_users=s.query(func.count(User.user_id)).scalar(),
            new_today=s.query(func.count(User.user_id)).filter(User.joined_at == today_iran().isoformat()).scalar(),
            configs=s.query(func.count(UserConfig.id)).scalar(),
            total_rev=revenue(), month_rev=revenue(Request.answered_at.like(f"{month}%")),
            day_rev=revenue(Request.answered_at.like(f"{today}%")),
            day_sales=sales_count(Request.answered_at.like(f"{today}%")),
            charged_today=charged_today,
            unanswered=s.query(func.count(Request.id)).filter(Request.status == "unanswered").scalar(),
            processed=s.query(func.count(Request.id)).filter(Request.status != "unanswered").scalar(),
            open_tickets=s.query(func.count(Ticket.id)).filter(Ticket.status == "open").scalar(),
            avg_rating=float(s.query(func.avg(Rating.score)).scalar() or 0),
            rating_count=int(s.query(func.count(Rating.id)).scalar() or 0),
        )


@dp.callback_query(F.data == "admin_sales_stats", MainAdmin())
async def admin_sales_stats_handler(call: types.CallbackQuery):
    await call.answer()
    n = stats_numbers()
    out = (
        "📊 <b>آمار جامع و وضعیت درخواست‌ها:</b>\n\n"
        f"👥 کل کاربران: {n['total_users']} (امروز جدید: {n['new_today']})\n"
        f"📦 کل سرویس‌های فعال: {n['configs']}\n"
        f"💰 درآمد کل: {n['total_rev']:,} تومان\n\n"
        f"📅 <b>ماه جاری ({n['month']}):</b> {n['month_rev']:,} تومان\n"
        f"☀️ <b>امروز ({n['today']}):</b> {n['day_rev']:,} تومان در {n['day_sales']} فروش\n\n"
        f"🔴 در انتظار پاسخ: {n['unanswered']}\n"
        f"🟢 بررسی‌شده: {n['processed']}\n"
        f"🎫 تیکت باز: {n['open_tickets']}\n"
        f"⭐ میانگین امتیاز: {n['avg_rating']:.2f} ({n['rating_count']} نظر)"
    )
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                b(text="📈 نمودار هفتگی", callback_data="sreport_week"),
                b(text="📈 نمودار ماهانه", callback_data="sreport_month"),
            ],
            [b(text="🔙 بازگشت به پنل", callback_data="admin_home")],
        ]
    )
    await edit_or_send(call, out, markup)


SALE_TYPES = ["buy", "renew"]


def _rev_for_prefixes(s, prefixes):
    conds = [Request.answered_at.like(f"{p}%") for p in prefixes]
    total, cnt = (
        s.query(func.coalesce(func.sum(Request.price), 0), func.count(Request.id))
        .filter(Request.status == "answered", Request.req_type.in_(SALE_TYPES), or_(*conds))
        .one()
    )
    return int(total or 0), int(cnt or 0)


def _bar(value, maxv, width=12):
    if maxv <= 0 or value <= 0:
        return "▫️"
    return "█" * max(1, round(value / maxv * width))


def build_sales_report(period):
    today = jalali_today()
    if period == "week":
        title = "۷ روز اخیر"
        buckets = []
        for i in range(6, -1, -1):
            d = jdatetime.date.fromgregorian(date=today_iran() - timedelta(days=i))
            buckets.append((d.strftime("%m/%d"), [d.strftime("%Y/%m/%d")]))
        rank_prefixes = [p for _label, ps in buckets for p in ps]
    else:
        title = "۶ ماه اخیر"
        buckets = []
        idx = today.year * 12 + today.month - 1
        for i in range(5, -1, -1):
            y, m0 = divmod(idx - i, 12)
            buckets.append((f"{y}/{m0 + 1:02d}", [f"{y}/{m0 + 1:02d}"]))
        rank_prefixes = buckets[-1][1]

    with SessionLocal() as s:
        series = []
        for label, ps in buckets:
            total, cnt = _rev_for_prefixes(s, ps)
            series.append((label, total, cnt))
        cond = or_(*[Request.answered_at.like(f"{p}%") for p in rank_prefixes])
        base = (Request.status == "answered", Request.req_type.in_(SALE_TYPES), cond)
        plans = (
            s.query(Request.plan_name, func.count(Request.id), func.coalesce(func.sum(Request.price), 0))
            .filter(*base).group_by(Request.plan_name)
            .order_by(func.sum(Request.price).desc()).limit(5).all()
        )
        custs = (
            s.query(Request.user_id, func.count(Request.id), func.coalesce(func.sum(Request.price), 0))
            .filter(*base).group_by(Request.user_id)
            .order_by(func.sum(Request.price).desc()).limit(5).all()
        )
        names = {}
        if custs:
            for u in s.query(User).filter(User.user_id.in_([c[0] for c in custs])).all():
                names[u.user_id] = user_display(u)

    maxv = max([v for _l, v, _c in series] + [0])
    out = f"📈 <b>نمودار فروش {title}</b>\n\n"
    for label, v, c in series:
        out += f"<code>{label}</code> {_bar(v, maxv)} {v:,} ({c})\n"
    out += f"\n💰 جمع: {sum(v for _l, v, _c in series):,} تومان | 🧾 {sum(c for _l, _v, c in series)} فروش\n"
    scope = "۷ روز اخیر" if period == "week" else "ماه جاری"
    out += f"\n🏆 <b>پرفروش‌ترین پلن‌ها ({scope}):</b>\n"
    for i, (name, c, total) in enumerate(plans, 1):
        out += f"{i}. {esc(name or '')} — {c} فروش | {int(total):,}\n"
    if not plans:
        out += "—\n"
    out += f"\n👑 <b>بهترین مشتری‌ها ({scope}):</b>\n"
    for i, (uid, c, total) in enumerate(custs, 1):
        out += f"{i}. {esc(names.get(uid, str(uid)))} (<code>{uid}</code>) — {c} خرید | {int(total):,}\n"
    if not custs:
        out += "—\n"
    return out


@dp.callback_query(F.data.in_({"sreport_week", "sreport_month"}), MainAdmin())
async def admin_sales_report(call: types.CallbackQuery):
    await call.answer()
    period = "week" if call.data == "sreport_week" else "month"
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                b(text=("• " if period == "week" else "") + "هفتگی", callback_data="sreport_week"),
                b(text=("• " if period == "month" else "") + "ماهانه", callback_data="sreport_month"),
            ],
            [b(text="🔙 آمار کلی", callback_data="admin_sales_stats")],
        ]
    )
    await edit_or_send(call, build_sales_report(period), markup)


def daily_report_text():
    n = stats_numbers()
    return (
        f"📋 <b>گزارش روزانه {n['today']}</b>\n\n"
        f"💰 فروش امروز: {n['day_sales']} سفارش | {n['day_rev']:,} تومان\n"
        f"💳 شارژ کیف پول امروز: {n['charged_today']:,} تومان\n"
        f"👥 کاربر جدید: {n['new_today']} | کل کاربران: {n['total_users']}\n"
        f"🔴 درخواست در انتظار: {n['unanswered']}\n"
        f"🎫 تیکت باز: {n['open_tickets']}"
    )


@dp.callback_query(F.data == "admin_logs", MainAdmin())
async def admin_logs_handler(call: types.CallbackQuery):
    await call.answer()
    with SessionLocal() as s:
        rows = [(r.admin_id, r.action, r.created_at) for r in s.query(AdminLog).order_by(AdminLog.id.desc()).limit(20).all()]
    out = "🧾 <b>آخرین فعالیت‌های ادمین‌ها:</b>\n\n"
    for admin_id, action, created in rows:
        out += f"• {created} | <code>{admin_id}</code>\n  {esc(action or '')}\n"
    if not rows:
        out += "هنوز لاگی ثبت نشده است."
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="admin_home")]]
    )
    await edit_or_send(call, out[:4000], markup)


# ---------------------------------------------------------------------------
# پنل ادمین: پیام همگانی
# ---------------------------------------------------------------------------
BC_SEGMENTS = {
    "all": "👥 همه کاربران",
    "expired": "⌛ سرویس‌شان تمام شده",
    "nobuy": "🆕 بدون هیچ خرید",
    "active": "🟢 دارای سرویس فعال",
}
DIGITS_FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def segment_user_ids(s, seg):
    ok = or_(User.is_banned.is_(None), User.is_banned.is_(False))
    base = {r[0] for r in s.query(User.user_id).filter(ok).all()}
    if seg == "all":
        return sorted(base)
    today_iso = today_iran().isoformat()
    active = {
        r[0]
        for r in s.query(UserConfig.user_id)
        .filter(or_(UserConfig.expires_at.is_(None), UserConfig.expires_at >= today_iso))
        .all()
    }
    if seg == "active":
        return sorted(base & active)
    if seg == "expired":
        expired = {
            r[0] for r in s.query(UserConfig.user_id).filter(UserConfig.expires_at < today_iso).all()
        }
        return sorted((base & expired) - active)
    if seg == "nobuy":
        buyers = {
            r[0]
            for r in s.query(Request.user_id)
            .filter(Request.status == "answered", Request.req_type.in_(SALE_TYPES))
            .all()
        }
        return sorted(base - buyers)
    return []


async def run_broadcast(bot, from_chat, message_id, seg):
    """پیام را با copy_message برای بخش انتخابی می‌فرستد؛ (موفق, بلاک, ناموفق)."""
    with SessionLocal() as s:
        ids = segment_user_ids(s, seg)
    sent = blocked = failed = 0
    for u_id in ids:
        try:
            await bot.copy_message(chat_id=u_id, from_chat_id=from_chat, message_id=message_id)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.copy_message(chat_id=u_id, from_chat_id=from_chat, message_id=message_id)
                sent += 1
            except Exception:  # noqa: BLE001
                failed += 1
        except Exception:  # noqa: BLE001
            failed += 1
        await asyncio.sleep(0.05)
    return sent, blocked, failed


async def _broadcast_and_report(bot, report_chat, from_chat, message_id, seg, admin_id, prefix=""):
    sent, blocked, failed = await run_broadcast(bot, from_chat, message_id, seg)
    log_action(admin_id, f"پیام همگانی ({BC_SEGMENTS.get(seg, seg)}): {sent} موفق")
    try:
        await bot.send_message(
            chat_id=report_chat,
            text=(
                f"{prefix}✅ پیام همگانی ({esc(BC_SEGMENTS.get(seg, seg))}) ارسال شد!\n\n"
                f"📨 ارسال‌شده به: {sent} کاربر\n🚫 مسدود شده توسط کاربر: {blocked}\n⚠️ ناموفق: {failed}"
            ),
            reply_markup=get_admin_main_keyboard(admin_id),
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("broadcast report failed: %s", e)
    return f"{sent} موفق / {blocked} بلاک / {failed} ناموفق"


def parse_run_time(raw):
    """'HH:MM' (نزدیک‌ترین زمان آینده) یا 'YYYY/MM/DD HH:MM' شمسی. خروجی: datetime ساده به وقت ایران یا None."""
    t = (raw or "").strip().translate(DIGITS_FA)
    now = now_iran().replace(tzinfo=None)
    try:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
        if m:
            cand = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            if cand <= now:
                cand += timedelta(days=1)
            return cand
        m = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", t)
        if m:
            g = jdatetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).togregorian()
            cand = datetime(g.year, g.month, g.day, int(m.group(4)), int(m.group(5)))
            return cand if cand > now else None
    except (ValueError, OverflowError):
        return None
    return None


@dp.callback_query(F.data == "admin_broadcast_start", MainAdmin())
async def admin_broadcast_start_handler(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    b = InlineKeyboardButton
    with SessionLocal() as s:
        counts = {k: len(segment_user_ids(s, k)) for k in BC_SEGMENTS}
    rows = [[b(text=f"{label} ({counts[k]})", callback_data=f"bcseg_{k}")] for k, label in BC_SEGMENTS.items()]
    rows.append([b(text="⏰ پیام‌های زمان‌بندی‌شده", callback_data="bc_scheduled")])
    rows.append([b(text="🔙 بازگشت به پنل", callback_data="admin_home")])
    await edit_or_send(
        call, "📢 <b>پیام همگانی</b>\n\nگروه مخاطب را انتخاب کنید:", InlineKeyboardMarkup(inline_keyboard=rows)
    )


@dp.callback_query(F.data.startswith("bcseg_"), MainAdmin())
async def admin_broadcast_segment(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    seg = call.data.split("_", 1)[1]
    if seg not in BC_SEGMENTS:
        return
    await state.set_state(PaymentState.broadcasting)
    await state.update_data(bc_seg=seg)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="انصراف", callback_data="admin_broadcast_start")]])
    await edit_or_send(
        call,
        f"📢 گروه: <b>{esc(BC_SEGMENTS[seg])}</b>\n\nپیام، متن یا رسانه مورد نظر را بفرستید:",
        markup,
    )


@dp.message(PaymentState.broadcasting, MainAdmin())
async def process_broadcast_handler(message: types.Message, state: FSMContext):
    data = await state.get_data()
    seg = data.get("bc_seg", "all")
    await state.set_state(None)
    await state.update_data(bc_chat=message.chat.id, bc_msg=message.message_id, bc_seg=seg)
    with SessionLocal() as s:
        count = len(segment_user_ids(s, seg))
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="📤 ارسال همین الان", callback_data="bc_now")],
            [b(text="⏰ زمان‌بندی ارسال", callback_data="bc_sched")],
            [b(text="❌ انصراف", callback_data="admin_broadcast_start")],
        ]
    )
    await message.answer(
        f"پیام دریافت شد. مخاطب: <b>{esc(BC_SEGMENTS.get(seg, seg))}</b> ({count} نفر)\nچطور ارسال شود؟",
        reply_markup=markup,
    )


@dp.callback_query(F.data == "bc_now", MainAdmin())
async def broadcast_now(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("bc_msg"):
        await call.answer("درخواست منقضی شده است.", show_alert=True)
        return
    await call.answer()
    await state.clear()
    await edit_or_send(call, "⏳ ارسال پیام همگانی آغاز شد؛ پس از اتمام گزارش می‌آید.")
    asyncio.create_task(
        _broadcast_and_report(
            call.bot, call.message.chat.id, data["bc_chat"], data["bc_msg"], data.get("bc_seg", "all"),
            call.from_user.id,
        )
    )


@dp.callback_query(F.data == "bc_sched", MainAdmin())
async def broadcast_schedule_start(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("bc_msg"):
        await call.answer("درخواست منقضی شده است.", show_alert=True)
        return
    await call.answer()
    await state.set_state(PaymentState.broadcast_time)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="انصراف", callback_data="admin_broadcast_start")]])
    await edit_or_send(
        call,
        "⏰ زمان ارسال را بفرستید (به وقت ایران):\n\n"
        "• فقط ساعت، مثلاً <code>18:30</code> (نزدیک‌ترین زمان آینده)\n"
        "• یا تاریخ شمسی کامل، مثلاً <code>1405/08/20 18:30</code>\n\n"
        "⚠️ پیام اصلی را از چت خود پاک نکنید، چون ربات در زمان مقرر از روی آن کپی می‌کند.",
        markup,
    )


@dp.message(PaymentState.broadcast_time, MainAdmin())
async def broadcast_schedule_save(message: types.Message, state: FSMContext):
    when = parse_run_time(message.text or "")
    if when is None:
        await message.answer("زمان نامعتبر یا گذشته است. مثال: 18:30 یا 1405/08/20 18:30")
        return
    data = await state.get_data()
    seg = data.get("bc_seg", "all")
    with SessionLocal() as s:
        s.add(
            ScheduledBroadcast(
                segment=seg, chat_id=data["bc_chat"], message_id=data["bc_msg"],
                run_at=when.strftime("%Y-%m-%dT%H:%M"), status="pending",
                created_by=message.from_user.id, created_at=get_current_jalali(),
            )
        )
        s.commit()
    await state.clear()
    jwhen = jdatetime.datetime.fromgregorian(datetime=when).strftime("%Y/%m/%d %H:%M")
    log_action(message.from_user.id, f"زمان‌بندی پیام همگانی برای {jwhen}")
    await message.answer(
        f"✅ پیام برای <b>{jwhen}</b> زمان‌بندی شد (گروه: {esc(BC_SEGMENTS.get(seg, seg))}).",
        reply_markup=get_admin_main_keyboard(message.from_user.id),
    )


@dp.callback_query(F.data == "bc_scheduled", MainAdmin())
async def broadcast_scheduled_list(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    with SessionLocal() as s:
        rows = [
            (x.id, x.segment, x.run_at)
            for x in s.query(ScheduledBroadcast)
            .filter(ScheduledBroadcast.status == "pending").order_by(ScheduledBroadcast.run_at).limit(20).all()
        ]
    out = "⏰ <b>پیام‌های زمان‌بندی‌شده:</b>\n\n"
    keyboard = []
    for bid, seg, run_at in rows:
        try:
            jw = jdatetime.datetime.fromgregorian(datetime=datetime.strptime(run_at, "%Y-%m-%dT%H:%M")).strftime(
                "%Y/%m/%d %H:%M"
            )
        except ValueError:
            jw = run_at
        out += f"#{bid} | {jw} | {esc(BC_SEGMENTS.get(seg, seg))}\n"
        keyboard.append([InlineKeyboardButton(text=f"🗑 لغو #{bid}", callback_data=f"bcdel_{bid}")])
    if not rows:
        out += "موردی وجود ندارد."
    keyboard.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_broadcast_start")])
    await edit_or_send(call, out, InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data.startswith("bcdel_"), MainAdmin())
async def broadcast_scheduled_cancel(call: types.CallbackQuery, state: FSMContext):
    bid = int(call.data.split("_")[1])
    with SessionLocal() as s:
        x = s.get(ScheduledBroadcast, bid)
        if x is not None and x.status == "pending":
            x.status = "cancelled"
            s.commit()
    log_action(call.from_user.id, f"لغو پیام زمان‌بندی‌شده #{bid}")
    await broadcast_scheduled_list(call, state)


# ---------------------------------------------------------------------------
# پنل ادمین: تنظیمات ربات (عمومی و داده‌محور)
# ---------------------------------------------------------------------------
def setting_display(key):
    meta, value = SETTINGS[key], get_setting(key)
    t = meta["type"]
    if t == "toggle":
        return "✅ روشن" if value in ("1", "true", "True") else "⬜ خاموش"
    if t in ("photo", "sticker"):
        return "✅ تنظیم شده" if value else "—"
    if not value:
        return "—"
    return esc(value if len(value) <= 40 else value[:40] + "…")


def render_settings_page(page):
    out = f"<b>{SETTING_PAGES[page]}</b>\n\n"
    keyboard = []
    for key, meta in SETTINGS.items():
        if meta["page"] != page:
            continue
        out += f"• {meta['label']}: {setting_display(key)}\n"
        if meta["type"] == "toggle":
            label = ("🔴 خاموش: " if get_bool(key) else "🟢 روشن: ") + meta["label"]
        else:
            label = "✏️ " + meta["label"]
        keyboard.append([InlineKeyboardButton(text=label[:60], callback_data=f"sedit_{key}")])
    if page == "chpost":
        keyboard.append([InlineKeyboardButton(text="📤 ارسال همین الان به کانال", callback_data="chpost_now")])
    if page == "mon":
        keyboard.append(
            [
                InlineKeyboardButton(text="📣 ارسال اعلان قطعی", callback_data="outage_send"),
                InlineKeyboardButton(text="✅ ارسال اعلان وصل شدن", callback_data="recovery_send"),
            ]
        )
    keyboard.append([InlineKeyboardButton(text="🔙 بازگشت به تنظیمات", callback_data="admin_settings")])
    return out, InlineKeyboardMarkup(inline_keyboard=keyboard)


@dp.callback_query(F.data == "chpost_now", MainAdmin())
async def chpost_now(call: types.CallbackQuery):
    try:
        await post_to_channel(call.bot)
        await call.answer("✅ در کانال ارسال شد.", show_alert=True)
    except Exception as e:  # noqa: BLE001
        await call.answer(f"ناموفق: {e}"[:190], show_alert=True)


@dp.callback_query(F.data.in_({"outage_send", "recovery_send"}), MainAdmin())
async def manual_outage_send(call: types.CallbackQuery):
    down = call.data == "outage_send"
    asyncio.create_task(broadcast_text(call.bot, outage_message(down)))
    log_action(call.from_user.id, "ارسال دستی اعلان " + ("قطعی" if down else "وصل شدن"))
    await call.answer("ارسال برای همه کاربران آغاز شد.", show_alert=True)


@dp.callback_query(F.data == "admin_settings", MainAdmin())
async def admin_settings_menu(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    keyboard = [[InlineKeyboardButton(text=title, callback_data=f"spage_{page}")] for page, title in SETTING_PAGES.items()]
    keyboard.append([InlineKeyboardButton(text="👮 مدیریت ادمین‌ها", callback_data="sadmins")])
    keyboard.append([InlineKeyboardButton(text="🔙 بازگشت به پنل", callback_data="admin_home")])
    await edit_or_send(call, "⚙️ <b>تنظیمات ربات</b>\nیک بخش را انتخاب کنید:", InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data.startswith("spage_"), MainAdmin())
async def admin_settings_page(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    page = call.data.split("_", 1)[1]
    if page not in SETTING_PAGES:
        return
    out, markup = render_settings_page(page)
    await edit_or_send(call, out, markup)


@dp.callback_query(F.data.startswith("sedit_"), MainAdmin())
async def admin_setting_edit(call: types.CallbackQuery, state: FSMContext):
    key = call.data[len("sedit_"):]
    meta = SETTINGS.get(key)
    if meta is None:
        await call.answer()
        return
    await call.answer()
    if meta["type"] == "toggle":
        new_value = "0" if get_bool(key) else "1"
        set_setting(key, new_value)
        log_action(call.from_user.id, f"تنظیم {key} = {new_value}")
        out, markup = render_settings_page(meta["page"])
        await edit_or_send(call, out, markup)
        return

    hints = {
        "num": f"یک عدد بین {meta.get('min', 0)} تا {meta.get('max', 0)} بفرستید.",
        "text": "متن جدید را بفرستید. برای پاک کردن، - بفرستید.",
        "photo": "یک عکس بفرستید. برای حذف، - بفرستید.",
        "sticker": "یک استیکر بفرستید. برای حذف، - بفرستید.",
    }
    await state.set_state(PaymentState.editing_setting)
    await state.update_data(setting_key=key)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="انصراف", callback_data=f"spage_{meta['page']}")]]
    )
    await edit_or_send(call, f"✏️ <b>{meta['label']}</b>\n\n{hints[meta['type']]}", markup)


@dp.message(PaymentState.editing_setting, MainAdmin())
async def admin_setting_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("setting_key")
    meta = SETTINGS.get(key)
    if meta is None:
        await state.clear()
        return

    t, value = meta["type"], None
    text_in = (message.text or "").strip()
    if t == "num":
        raw = text_in.replace(",", "")
        if not raw.isdigit() or not (meta["min"] <= int(raw) <= meta["max"]):
            await message.answer(f"عدد معتبر بین {meta['min']} تا {meta['max']} بفرستید.")
            return
        value = str(int(raw))
    elif t == "text":
        if not text_in:
            await message.answer("لطفاً متن بفرستید.")
            return
        value = "" if text_in == "-" else text_in
        if key == "charge_amounts" and value:
            nums = [p for p in re.split(r"[,\s،]+", value) if p.isdigit() and int(p) > 0]
            if not nums or len(nums) > 12:
                await message.answer("چند عدد (حداکثر ۱۲ تا) با کاما جدا کنید. مثال: 200000,400000")
                return
            value = ",".join(nums)
        if key == "post_times" and value:
            times = parse_times(value)
            if not times:
                await message.answer("ساعت نامعتبر است. مثال: 10:00,20:00")
                return
            value = ",".join(times)
        if key == "monitor_urls" and value:
            urls = parse_urls(value)
            if not urls:
                await message.answer("آدرس معتبر http/https بفرستید. مثال: https://example.com")
                return
            value = ",".join(urls)
        if key in ("card_number", "card_holder") and not value:
            await message.answer("این مورد نمی‌تواند خالی باشد.")
            return
    elif t == "photo":
        if message.photo:
            value = message.photo[-1].file_id
        elif text_in == "-":
            value = ""
        else:
            await message.answer("لطفاً یک عکس بفرستید یا - برای حذف.")
            return
    elif t == "sticker":
        if message.sticker:
            value = message.sticker.file_id
        elif text_in == "-":
            value = ""
        else:
            await message.answer("لطفاً یک استیکر بفرستید یا - برای حذف.")
            return

    set_setting(key, value)
    await state.clear()
    log_action(message.from_user.id, f"ویرایش تنظیم {key}")
    out, markup = render_settings_page(meta["page"])
    await message.answer("✅ ذخیره شد.\n\n" + out, reply_markup=markup)


# --- مدیریت ادمین‌ها ---
async def _render_subadmins(call):
    ids = sub_admins()
    out = (
        "👮 <b>مدیریت ادمین‌ها</b>\n\n"
        "ادمین‌های فرعی فقط می‌توانند درخواست‌ها (رسید و شارژ) و تیکت‌ها را مدیریت کنند؛ "
        "به تنظیمات، کاربران و موجودی دسترسی ندارند.\n\n"
        f"ادمین اصلی: <code>{ADMIN_ID}</code>\n"
    )
    keyboard = []
    for i in ids:
        out += f"• <code>{i}</code>\n"
        keyboard.append([InlineKeyboardButton(text=f"🗑 حذف {i}", callback_data=f"sadmindel_{i}")])
    if not ids:
        out += "• ادمین فرعی ثبت نشده است.\n"
    keyboard.append([InlineKeyboardButton(text="➕ افزودن ادمین", callback_data="sadmin_add")])
    keyboard.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_settings")])
    await edit_or_send(call, out, InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data == "sadmins", MainAdmin())
async def sadmins_menu(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await _render_subadmins(call)


@dp.callback_query(F.data == "sadmin_add", MainAdmin())
async def sadmin_add_start(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(PaymentState.adding_subadmin)
    await edit_or_send(call, "آیدی عددی تلگرام ادمین جدید را بفرستید (او باید قبلاً ربات را استارت کرده باشد):")


@dp.message(PaymentState.adding_subadmin, MainAdmin())
async def sadmin_add_save(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("آیدی عددی معتبر بفرستید.")
        return
    new_id = int(raw)
    ids = sub_admins()
    if new_id != ADMIN_ID and new_id not in ids:
        ids.append(new_id)
        set_setting("sub_admins", ",".join(str(i) for i in ids))
        log_action(message.from_user.id, f"افزودن ادمین فرعی {new_id}")
    await state.clear()
    await message.answer("✅ ادمین فرعی ثبت شد.", reply_markup=get_admin_main_keyboard(message.from_user.id))


@dp.callback_query(F.data.startswith("sadmindel_"), MainAdmin())
async def sadmin_delete(call: types.CallbackQuery):
    await call.answer()
    rm = int(call.data.split("_")[1])
    ids = [i for i in sub_admins() if i != rm]
    set_setting("sub_admins", ",".join(str(i) for i in ids))
    log_action(call.from_user.id, f"حذف ادمین فرعی {rm}")
    await _render_subadmins(call)


# ---------------------------------------------------------------------------
# بکاپ و بازیابی
# ---------------------------------------------------------------------------
def build_backup_bytes():
    payload = {"version": 1, "created_at": get_current_jalali(), "tables": {}}
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            rows = conn.execute(table.select()).mappings().all()
            payload["tables"][table.name] = [dict(r) for r in rows]
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def parse_backup(raw_bytes):
    payload = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("tables"), dict):
        raise ValueError("ساختار فایل بکاپ معتبر نیست.")
    names = {t.name for t in Base.metadata.sorted_tables}
    tables = payload["tables"]
    if not set(tables.keys()) <= names or not isinstance(tables.get("users"), list):
        raise ValueError("این فایل بکاپ ربات Swan VPN نیست.")
    for rows in tables.values():
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise ValueError("ساختار جدول‌ها در فایل بکاپ معتبر نیست.")
    return payload


def apply_backup(payload):
    tables = payload["tables"]
    ordered = list(Base.metadata.sorted_tables)
    with engine.begin() as conn:
        for t in reversed(ordered):
            conn.execute(t.delete())
        for t in ordered:
            rows = tables.get(t.name, [])
            cols = list(t.c.keys())
            clean = [{c: row.get(c) for c in cols} for row in rows]
            if clean:
                conn.execute(t.insert(), clean)
        if engine.dialect.name == "postgresql":
            for t in ordered:
                if "id" in t.c:
                    try:
                        with conn.begin_nested():
                            conn.execute(
                                text(
                                    f"SELECT setval(pg_get_serial_sequence('{t.name}', 'id'), "
                                    f"COALESCE((SELECT MAX(id) FROM {t.name}), 1), "
                                    f"(SELECT MAX(id) FROM {t.name}) IS NOT NULL)"
                                )
                            )
                    except Exception as e:  # noqa: BLE001
                        logging.warning("sequence reset skipped for %s: %s", t.name, e)
    _cache.clear()


async def send_backup(bot, chat_id, caption):
    data = build_backup_bytes()
    name = f"swan_backup_{now_iran().strftime('%Y%m%d_%H%M')}.json"
    await bot.send_document(chat_id=chat_id, document=BufferedInputFile(data, filename=name), caption=caption)


@dp.callback_query(F.data == "admin_backup", MainAdmin())
async def admin_backup_menu(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="📤 دریافت بکاپ همین الان", callback_data="backup_now")],
            [b(text="📥 بازیابی از فایل بکاپ", callback_data="backup_restore")],
            [b(text="🔙 بازگشت به پنل", callback_data="admin_home")],
        ]
    )
    await edit_or_send(
        call,
        "🧰 <b>بکاپ دیتابیس</b>\n\nبکاپ شامل کاربران، موجودی‌ها، سرویس‌ها، درخواست‌ها و تنظیمات است. "
        "فایل را جای امنی نگه دارید.",
        markup,
    )


@dp.callback_query(F.data == "backup_now", MainAdmin())
async def backup_now(call: types.CallbackQuery):
    await call.answer("در حال ساخت بکاپ...")
    try:
        await send_backup(call.bot, call.message.chat.id, f"🧰 بکاپ دستی - {get_current_jalali()}")
        log_action(call.from_user.id, "دریافت بکاپ دستی")
    except Exception as e:  # noqa: BLE001
        await call.message.answer(f"❌ ساخت بکاپ ناموفق بود: {esc(str(e))}")


@dp.callback_query(F.data == "backup_restore", MainAdmin())
async def backup_restore_start(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(PaymentState.restore_wait_file)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="انصراف", callback_data="admin_backup")]])
    await edit_or_send(
        call,
        "📥 فایل بکاپ (.json) را همین‌جا بفرستید.\n"
        "⚠️ با بازیابی، اطلاعات فعلی دیتابیس با محتوای فایل جایگزین می‌شود.",
        markup,
    )


@dp.message(PaymentState.restore_wait_file, MainAdmin(), F.document)
async def backup_restore_file(message: types.Message, state: FSMContext):
    buf = io.BytesIO()
    try:
        await message.bot.download(message.document.file_id, destination=buf)
        payload = parse_backup(buf.getvalue())
    except Exception as e:  # noqa: BLE001
        await message.answer(f"❌ فایل نامعتبر است: {esc(str(e))}")
        return

    summary = "\n".join(f"• {name}: {len(rows)}" for name, rows in payload["tables"].items())
    await state.update_data(restore_file_id=message.document.file_id)
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [b(text="✅ بله، بازیابی کن", callback_data="restore_confirm")],
            [b(text="انصراف", callback_data="admin_backup")],
        ]
    )
    await message.answer(
        f"فایل بکاپ ({esc(str(payload.get('created_at', '')))}) خوانده شد:\n{summary}\n\n"
        "⚠️ مطمئنید که اطلاعات فعلی با این بکاپ جایگزین شود؟",
        reply_markup=markup,
    )


@dp.message(PaymentState.restore_wait_file, MainAdmin())
async def backup_restore_not_file(message: types.Message):
    await message.answer("لطفاً فایل بکاپ را به صورت فایل (document) بفرستید.")


@dp.callback_query(F.data == "restore_confirm", MainAdmin())
async def backup_restore_confirm(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    file_id = data.get("restore_file_id")
    if not file_id:
        await call.answer("درخواست منقضی شده است.", show_alert=True)
        return
    await call.answer("در حال بازیابی...")
    try:
        buf = io.BytesIO()
        await call.bot.download(file_id, destination=buf)
        payload = parse_backup(buf.getvalue())
        apply_backup(payload)
    except Exception as e:  # noqa: BLE001
        await call.message.answer(f"❌ بازیابی ناموفق بود (اطلاعات قبلی دست‌نخورده ماند): {esc(str(e))}")
        return
    await state.clear()
    log_action(call.from_user.id, "بازیابی دیتابیس از بکاپ")
    await call.message.answer("✅ بازیابی با موفقیت انجام شد.", reply_markup=get_admin_main_keyboard(call.from_user.id))


# ---------------------------------------------------------------------------
# کارهای زمان‌بندی‌شده: یادآوری تمدید، گزارش روزانه، بکاپ شبانه
# ---------------------------------------------------------------------------
async def job_reminders(bot):
    days = get_int("reminder_days")
    todo = []
    with SessionLocal() as s:
        rows = (
            s.query(UserConfig)
            .filter(UserConfig.expires_at.isnot(None), or_(UserConfig.reminded.is_(None), UserConfig.reminded.is_(False)))
            .all()
        )
        for c in rows:
            try:
                left = (date.fromisoformat(c.expires_at) - today_iran()).days
            except (TypeError, ValueError):
                continue
            if left <= days:
                todo.append((c.id, c.user_id, c.config_name, left, c.service_id))
                c.reminded = True
        s.commit()

    for cfg_id, uid, name, left, sid in todo:
        if left >= 0:
            msg = f"⏰ سرویس «{esc(name or '')}» شما {left} روز دیگر تمام می‌شود. برای تمدید روی دکمه زیر بزنید."
        else:
            msg = f"⚠️ سرویس «{esc(name or '')}» شما منقضی شده است. برای تمدید روی دکمه زیر بزنید."
        markup = None
        if sid:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔄 تمدید سرویس", callback_data=f"renew_{cfg_id}")]]
            )
        try:
            await bot.send_message(chat_id=uid, text=msg, reply_markup=markup)
        except Exception as e:  # noqa: BLE001
            logging.info("reminder to %s failed: %s", uid, e)
        await asyncio.sleep(0.05)


async def job_report(bot):
    if get_bool("report_on"):
        await bot.send_message(chat_id=ADMIN_ID, text=daily_report_text())


async def job_backup(bot):
    if get_bool("backup_on"):
        await send_backup(bot, ADMIN_ID, f"🧰 بکاپ شبانه خودکار - {get_current_jalali()}")


# ---------------------------------------------------------------------------
# مانیتور سرور و اعلان قطعی
# ---------------------------------------------------------------------------
_monitor = {"fails": {}, "down": False}


def parse_urls(raw):
    out = []
    for part in re.split(r"[,\s،]+", raw or ""):
        part = part.strip()
        if re.match(r"^https?://\S+$", part) and part not in out:
            out.append(part)
    return out


async def check_url(session, url):
    try:
        async with session.get(url, allow_redirects=True) as resp:
            return resp.status < 500
    except Exception:  # noqa: BLE001
        return False


async def broadcast_text(bot, text_):
    with SessionLocal() as s:
        ids = segment_user_ids(s, "all")
    for uid in ids:
        try:
            await bot.send_message(chat_id=uid, text=text_)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.05)


def outage_message(down):
    key = "outage_text" if down else "recovery_text"
    return esc(get_setting(key).strip() or DEFAULTS[key])


async def announce_outage(bot, down, bad_urls=None):
    if down:
        detail = "\n".join(f"• {esc(u)}" for u in (bad_urls or []))
        await notify_staff(bot, f"🔴 <b>مانیتور: قطعی تشخیص داده شد</b>\n{detail}")
    else:
        await notify_staff(bot, "🟢 <b>مانیتور: سرورها دوباره در دسترس‌اند.</b>")
    if get_bool("monitor_notify_users"):
        asyncio.create_task(broadcast_text(bot, outage_message(down)))


async def monitor_loop(bot):
    async with ClientSession(timeout=ClientTimeout(total=10)) as session:
        while True:
            await asyncio.sleep(60)
            try:
                if not get_bool("monitor_on"):
                    continue
                urls = parse_urls(get_setting("monitor_urls"))
                if not urls:
                    continue
                results = await asyncio.gather(*(check_url(session, u) for u in urls))
                for u, ok in zip(urls, results):
                    _monitor["fails"][u] = 0 if ok else _monitor["fails"].get(u, 0) + 1
                threshold = max(1, get_int("monitor_fails"))
                bad = [u for u in urls if _monitor["fails"].get(u, 0) >= threshold]
                if bad and not _monitor["down"]:
                    _monitor["down"] = True
                    await announce_outage(bot, True, bad)
                elif not bad and _monitor["down"]:
                    _monitor["down"] = False
                    await announce_outage(bot, False)
            except Exception as e:  # noqa: BLE001
                logging.error("monitor error: %s", e)


# ---------------------------------------------------------------------------
# پست خودکار لیست تعرفه در کانال
# ---------------------------------------------------------------------------
def parse_times(raw):
    out = []
    for part in re.split(r"[,\s،]+", (raw or "").translate(DIGITS_FA)):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", part)
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            t = f"{int(m.group(1)):02d}:{m.group(2)}"
            if t not in out:
                out.append(t)
    return out


def channel_post_text():
    general = get_int("discount_percent")
    with SessionLocal() as s:
        rows = [(r.category, r.name, r.price) for r in s.query(Service).order_by(Service.category, Service.id).all()]
    title = get_setting("post_title").strip() or "📊 لیست تعرفه Swan VPN"
    out = f"<b>{esc(title)}</b>\n"
    if general > 0:
        out += f"\n🎁 تخفیف ویژه {general}٪ فعال است!\n"
    current = None
    for cat, name, price in rows:
        if cat != current:
            current = cat
            out += f"\n📂 <b>{esc(CATEGORY_LABELS.get(cat, cat))}:</b>\n"
        out += price_line(name, price, price_with_percent(price, general))
    out += f"\n🤖 برای خرید: @{BOT_USERNAME}"
    return out


def channel_chat_id():
    ch = get_setting("post_channel").strip()
    if not ch:
        raise ValueError("کانال تنظیم نشده است.")
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if ch.startswith(prefix):
            ch = "@" + ch[len(prefix):].strip("/")
    if re.fullmatch(r"-?\d+", ch):
        return int(ch)
    return ch if ch.startswith("@") else "@" + ch


async def post_to_channel(bot):
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🛒 شروع خرید", url=f"https://t.me/{BOT_USERNAME}")]]
    )
    await bot.send_message(chat_id=channel_chat_id(), text=channel_post_text(), reply_markup=markup)


async def job_channel_posts(bot):
    if not get_bool("post_on") or not get_setting("post_channel").strip():
        return
    now = now_iran()
    today = now.date().isoformat()
    for t in parse_times(get_setting("post_times")):
        h, m = (int(x) for x in t.split(":"))
        diff = (now.hour * 60 + now.minute) - (h * 60 + m)
        if 0 <= diff <= 10 and get_setting(f"last_post_{t}") != today:
            set_setting(f"last_post_{t}", today)
            try:
                await post_to_channel(bot)
            except Exception as e:  # noqa: BLE001
                logging.error("channel post failed: %s", e)
                try:
                    await bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"❌ پست خودکار کانال ناموفق بود: {esc(str(e))}\n"
                        "مطمئن شوید ربات در کانال ادمین است و اجازه ارسال پیام دارد.",
                    )
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# اجرای پیام‌های همگانی زمان‌بندی‌شده
# ---------------------------------------------------------------------------
async def _run_scheduled_broadcast(bot, bid, seg, chat_id, msg_id, admin_id):
    result = await _broadcast_and_report(
        bot, admin_id, chat_id, msg_id, seg, admin_id, prefix=f"⏰ (زمان‌بندی #{bid}) "
    )
    with SessionLocal() as s:
        x = s.get(ScheduledBroadcast, bid)
        if x is not None:
            x.status = "sent"
            x.result = result
            s.commit()


async def job_scheduled_broadcasts(bot):
    now_s = now_iran().strftime("%Y-%m-%dT%H:%M")
    due = []
    with SessionLocal() as s:
        for x in s.query(ScheduledBroadcast).filter(
            ScheduledBroadcast.status == "pending", ScheduledBroadcast.run_at <= now_s
        ).all():
            x.status = "sending"
            due.append((x.id, x.segment, x.chat_id, x.message_id, x.created_by))
        s.commit()
    for bid, seg, chat_id, msg_id, admin_id in due:
        asyncio.create_task(_run_scheduled_broadcast(bot, bid, seg, chat_id, msg_id, admin_id))


JOBS = [("backup", 3, 0, job_backup), ("reminders", 10, 0, job_reminders), ("report", 23, 30, job_report)]


async def run_jobs(bot):
    now = now_iran()
    today = now.date().isoformat()
    for name, hour, minute, func_ in JOBS:
        if (now.hour, now.minute) < (hour, minute):
            continue
        if get_setting(f"last_{name}") == today:
            continue
        set_setting(f"last_{name}", today)
        try:
            await func_(bot)
        except Exception as e:  # noqa: BLE001
            logging.error("job %s failed: %s", name, e)


async def scheduler(bot):
    while True:
        try:
            await run_jobs(bot)
            await job_scheduled_broadcasts(bot)
            await job_channel_posts(bot)
        except Exception as e:  # noqa: BLE001
            logging.error("scheduler error: %s", e)
        await asyncio.sleep(60)


async def keepalive():
    """جلوگیری از خوابیدن سرویس رایگان Render با پینگ آدرس عمومی خودش."""
    url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if not url:
        return
    async with ClientSession(timeout=ClientTimeout(total=20)) as session:
        while True:
            await asyncio.sleep(600)
            try:
                async with session.get(url) as resp:
                    await resp.read()
            except Exception as e:  # noqa: BLE001
                logging.info("keepalive ping failed: %s", e)


# ---------------------------------------------------------------------------
# وب‌سرور سلامت و اجرای ربات
# ---------------------------------------------------------------------------
async def health_check(request):
    return web.Response(text="Bot is running", status=200)


async def update_bot_commands(bot):
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="منوی اصلی"),
            BotCommand(command="admin", description="پنل ادمین"),
        ]
    )


async def run_web_server(app, port):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    global BOT_USERNAME
    init_db()
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    await bot.delete_webhook(drop_pending_updates=True)
    await update_bot_commands(bot)

    app = web.Application()
    app.router.add_get("/", health_check)
    port = int(os.environ.get("PORT", 8080))
    await run_web_server(app, port)

    background = [
        asyncio.create_task(scheduler(bot)),
        asyncio.create_task(keepalive()),
        asyncio.create_task(monitor_loop(bot)),
    ]
    logging.info("Bot @%s is running; web server on port %s", BOT_USERNAME, port)
    try:
        await dp.start_polling(bot)
    finally:
        for task in background:
            task.cancel()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
