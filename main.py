import asyncio, hashlib, html, logging, os, re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import asyncpg
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton as B, InlineKeyboardMarkup as KB
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)

# ---------- CONFIG ----------
TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]
BASE_URL = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]


def clean_dsn(url):
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "channel_binding"]
    return urlunparse(p._replace(query=urlencode(q)))


DSN = clean_dsn(os.environ["DATABASE_URL"])
db: asyncpg.Pool = None
esc = html.escape

# ---------- TEXTS (ru, uk, en) ----------
LANGS = ("ru", "uk", "en")
T = {
    "choose": ("Выберите язык / Оберіть мову / Choose language",) * 3,
    "welcome": ("Привет! Здесь можно найти людей для взаимных рефералов.",
                "Привіт! Тут можна знайти людей для взаємних рефералів.",
                "Hi! Find people for mutual referrals here."),
    "menu": ("Меню:", "Меню:", "Menu:"),
    "b_new": ("➕ Новая анкета", "➕ Нова анкета", "➕ New listing"),
    "b_cat": ("🔎 Каталог", "🔎 Каталог", "🔎 Catalog"),
    "b_my": ("📋 Мои анкеты", "📋 Мої анкети", "📋 My listings"),
    "b_lang": ("🌐 Язык", "🌐 Мова", "🌐 Language"),
    "c_all": ("Все", "Усі", "All"),
    "c_bot": ("🤖 Бот", "🤖 Бот", "🤖 Bot"),
    "c_channel": ("📢 Канал", "📢 Канал", "📢 Channel"),
    "c_other": ("📦 Другое", "📦 Інше", "📦 Other"),
    "q_cat": ("Выберите категорию:", "Оберіть категорію:", "Choose a category:"),
    "q_title": ("Название (до 60 символов):", "Назва (до 60 символів):", "Title (up to 60 chars):"),
    "q_desc": ("Описание (до 300 символов):", "Опис (до 300 символів):", "Description (up to 300 chars):"),
    "q_link": ("Ваша реферальная ссылка (https://...):", "Ваше реферальне посилання (https://...):",
               "Your referral link (https://...):"),
    "bad": ("Нужна ссылка, начинающаяся с http:// или https://", "Потрібне посилання, що починається з http:// або https://",
            "A link starting with http:// or https:// is required"),
    "saved": ("✅ Анкета опубликована!", "✅ Анкету опубліковано!", "✅ Listing published!"),
    "empty": ("Пока ничего нет.", "Поки нічого немає.", "Nothing here yet."),
    "write": ("✉️ Написать", "✉️ Написати", "✉️ Message"),
    "report": ("⚠️ Пожаловаться", "⚠️ Поскаржитись", "⚠️ Report"),
    "thanks": ("Жалоба отправлена", "Скаргу надіслано", "Report sent"),
    "renew": ("🔄 Продлить", "🔄 Продовжити", "🔄 Renew"),
    "delete": ("🗑 Удалить", "🗑 Видалити", "🗑 Delete"),
    "done": ("Готово", "Готово", "Done"),
    "yours": ("Это ваша анкета", "Це ваша анкета", "This is your listing"),
    "hidden": ("(скрыта)", "(прихована)", "(hidden)"),
    "expiring": ("Анкета «{t}» скоро исчезнет. Продлить?", "Анкета «{t}» скоро зникне. Продовжити?",
                 "Listing “{t}” will expire soon. Renew?"),
    "sub": ("Чтобы смотреть следующие анкеты, подпишитесь на канал {ch}",
            "Щоб переглядати наступні анкети, підпішіться на канал {ch}",
            "To see more listings, subscribe to the channel: {ch}"),
    "b_sub": ("📢 Подписаться", "📢 Підписатися", "📢 Subscribe"),
    "cat_head": ("🔎 <b>Каталог</b>\n───────────\nЧто ищем сегодня?",
                 "🔎 <b>Каталог</b>\n───────────\nЩо шукаємо сьогодні?",
                 "🔎 <b>Catalog</b>\n───────────\nWhat are we looking for today?"),
    "new_head": ("➕ <b>Новая анкета</b>\n───────────\nШаг 1 из 4: выберите категорию",
                 "➕ <b>Нова анкета</b>\n───────────\nКрок 1 з 4: оберіть категорію",
                 "➕ <b>New listing</b>\n───────────\nStep 1 of 4: choose a category"),
    "check": ("✅ Я подписался", "✅ Я підписався", "✅ I subscribed"),
    "hint": ("Если кнопка не открывается — автор скрыл профиль.", "Якщо кнопка не відкривається — автор сховав профіль.",
             "If the button doesn't open, the author hid their profile."),
}


def t(lang, key, **kw):
    s = T[key][LANGS.index(lang)]
    return s.format(**kw) if kw else s


DEFAULTS = {"ttl_days": "30", "report_threshold": "3", "sub_required": "0", "sub_channel": "",
            "welcome_ru": "", "welcome_uk": "", "welcome_en": ""}


async def setting(k):
    v = await db.fetchval("select value from settings where key=$1", k)
    return DEFAULTS[k] if v is None else v


async def set_setting(k, v):
    await db.execute("insert into settings(key,value) values($1,$2) on conflict(key) do update set value=excluded.value", k, v)


async def sub_gate(bot, uid):
    """Returns the channel the user still has to join, or None if no subscription is required."""
    if uid in ADMIN_IDS or await setting("sub_required") != "1":
        return None
    ch = await setting("sub_channel")
    if not ch:
        return None
    try:
        cid = int(ch) if ch.lstrip("-").isdigit() else ch
        m = await bot.get_chat_member(cid, uid)
        return ch if m.status in ("left", "kicked") else None
    except Exception:
        logging.exception("subscription check failed (is the bot an admin of the channel?)")
        return None  # fail open, so a misconfigured channel does not lock everyone out


# ---------- DB ----------
SCHEMA = """
create table if not exists users(id bigint primary key, lang text, username text,
  banned boolean not null default false, created timestamptz not null default now());
create table if not exists listings(id serial primary key, owner bigint not null references users(id),
  category text not null, title text not null, descr text not null, link text not null,
  updated timestamptz not null default now(), status text not null default 'active',
  warned boolean not null default false, created timestamptz not null default now());
create table if not exists reports(listing_id int not null references listings(id) on delete cascade,
  reporter bigint not null, created timestamptz not null default now(), primary key(listing_id, reporter));
create table if not exists settings(key text primary key, value text not null);
alter table users add column if not exists last_seen timestamptz;
create table if not exists events(id bigserial primary key, uid bigint not null, kind text not null,
  created timestamptz not null default now());
create index if not exists events_created on events(created);
"""


async def ban_user(uid):
    await db.execute("insert into users(id, banned) values($1, true) on conflict(id) do update set banned=true", uid)
    await db.execute("delete from listings where owner=$1", uid)


async def log_event(uid, kind):
    await db.execute("insert into events(uid, kind) values($1, $2)", uid, kind)


async def cat_counts():
    rows = await db.fetch("select category, count(*) n from listings where status='active' group by category")
    return {x["category"]: x["n"] for x in rows}


def cat_kb(lang, cb, counts=None):
    """Category picker. cb(key) returns callback data. counts (catalog only) adds listing counts and an 'all' row."""
    def btn(k):
        label = t(lang, "c_" + k)
        if counts is not None:
            n = sum(counts.values()) if k == "all" else counts.get(k, 0)
            label += f" · {n}"
        return B(text=label, callback_data=cb(k))
    rows = [[btn("all")]] if counts is not None else []
    rows += [[btn("bot"), btn("channel")], [btn("other")], [B(text="🏠", callback_data="menu")]]
    return KB(inline_keyboard=rows)


# ---------- KEYBOARDS ----------
def lang_kb():
    return KB(inline_keyboard=[[B(text="🇷🇺 Русский", callback_data="sl:ru"),
                                B(text="🇺🇦 Українська", callback_data="sl:uk"),
                                B(text="🇬🇧 English", callback_data="sl:en")]])


def menu_kb(lang):
    def b(k, cb):
        return B(text=t(lang, k), callback_data=cb)
    return KB(inline_keyboard=[[b("b_cat", "cat")], [b("b_new", "new"), b("b_my", "my")], [b("b_lang", "lang")]])


# ---------- ILLUSTRATIONS ----------
ROOT_DIR = Path(__file__).parent
IMG_DIRS = [ROOT_DIR / "img", ROOT_DIR]  # works whether images are in img/ or next to main.py
_file_ids = {}


def find_img(name):
    for d in IMG_DIRS:
        p = d / f"{name}.png"
        if p.exists():
            return p
    return None


async def send_screen(bot, chat_id, name, text, kb=None):
    """Sends a message with an illustration on top (img/<name>.png). Falls back to plain text."""
    path = find_img(name)
    if path and len(text) <= 1024:
        fid = _file_ids.get(name)
        try:
            msg = await bot.send_photo(chat_id, fid or FSInputFile(path), caption=text, reply_markup=kb)
            if not fid:
                _file_ids[name] = msg.photo[-1].file_id
            return msg
        except Exception:
            logging.exception("send_photo failed, falling back to text")
    return await bot.send_message(chat_id, text, reply_markup=kb)


async def screen(event, name, text, kb=None):
    """event: Message or CallbackQuery. For callbacks the old message is removed, so the chat stays clean."""
    if isinstance(event, CallbackQuery):
        msg = event.message
        try:
            await msg.delete()
        except Exception:
            pass
        return await send_screen(event.bot, msg.chat.id, name, text, kb)
    return await send_screen(event.bot, event.chat.id, name, text, kb)


def home_kb():
    return KB(inline_keyboard=[[B(text="🏠", callback_data="menu")]])


# ---------- MIDDLEWARE ----------
class Mw(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = event.from_user
        if u is None:
            return await handler(event, data)
        guess = (u.language_code or "")[:2]
        guess = guess if guess in LANGS else None
        row = await db.fetchrow(
            """insert into users(id,lang,username,last_seen) values($1,$2,$3,now())
               on conflict(id) do update set username=excluded.username, last_seen=now() returning lang, banned""",
            u.id, guess, u.username)
        if row["banned"]:
            return
        is_cb = isinstance(event, CallbackQuery)
        msg = event.message if is_cb else event
        if row["lang"] is None and not (is_cb and event.data.startswith("sl:")):
            await send_screen(data["bot"], msg.chat.id, "lang", t("ru", "choose"), lang_kb())
            if is_cb:
                await event.answer()
            return
        lang = row["lang"] or "ru"
        data["lang"] = lang
        return await handler(event, data)


# ---------- STATES ----------
class New(StatesGroup):
    title = State()
    desc = State()
    link = State()


class Adm(StatesGroup):
    val = State()
    bc = State()


# ---------- USER ROUTER ----------
r = Router()


async def send_menu(target, lang, text=None, img="welcome"):
    await screen(target, img, text or t(lang, "menu"), menu_kb(lang))


@r.message(CommandStart())
async def start(m, lang, state: FSMContext):
    await state.clear()
    w = await setting("welcome_" + lang)
    await send_menu(m, lang, w or t(lang, "welcome"))


@r.message(Command("cancel"))
async def cancel(m, lang, state: FSMContext):
    await state.clear()
    await send_menu(m, lang)


@r.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, lang, state: FSMContext):
    await state.clear()
    await send_menu(c, lang)
    await c.answer()


@r.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


@r.callback_query(F.data == "lang")
async def lang_choose(c: CallbackQuery):
    await screen(c, "lang", t("ru", "choose"), lang_kb())
    await c.answer()


@r.callback_query(F.data.startswith("sl:"))
async def lang_set(c: CallbackQuery):
    lang = c.data[3:]
    if lang in LANGS:
        await db.execute("update users set lang=$2 where id=$1", c.from_user.id, lang)
        await send_menu(c, lang)
    await c.answer()


# --- create listing ---
@r.callback_query(F.data == "new")
async def new(c: CallbackQuery, lang, state: FSMContext):
    await state.clear()
    await screen(c, "new", t(lang, "new_head"), cat_kb(lang, lambda k: "nc:" + k))
    await c.answer()


@r.callback_query(F.data.startswith("nc:"))
async def new_cat(c: CallbackQuery, lang, state: FSMContext):
    await state.update_data(cat=c.data[3:])
    await state.set_state(New.title)
    await c.message.answer(t(lang, "q_title"))
    await c.answer()


@r.message(New.title, F.text)
async def new_title(m, lang, state: FSMContext):
    await state.update_data(title=m.text[:60])
    await state.set_state(New.desc)
    await m.answer(t(lang, "q_desc"))


@r.message(New.desc, F.text)
async def new_desc(m, lang, state: FSMContext):
    await state.update_data(desc=m.text[:300])
    await state.set_state(New.link)
    await m.answer(t(lang, "q_link"))


@r.message(New.link, F.text)
async def new_link(m, lang, state: FSMContext):
    link = m.text.strip()
    if not re.fullmatch(r"https?://\S{3,290}", link):
        return await m.answer(t(lang, "bad"))
    d = await state.get_data()
    await db.execute("insert into listings(owner,category,title,descr,link) values($1,$2,$3,$4,$5)",
                     m.from_user.id, d["cat"], d["title"], d["desc"], link)
    await log_event(m.from_user.id, "create")
    await state.clear()
    await send_menu(m, lang, t(lang, "saved"), "saved")


# --- catalog ---
@r.callback_query(F.data == "cat")
async def cat(c: CallbackQuery, lang):
    counts = await cat_counts()
    await screen(c, "catalog", t(lang, "cat_head"), cat_kb(lang, lambda k: f"p:{k}:0", counts))
    await c.answer()


@r.callback_query(F.data.startswith("p:"))
async def page(c: CallbackQuery, lang):
    _, cat_, p = c.data.split(":")
    if int(p) > 0:
        ch = await sub_gate(c.bot, c.from_user.id)
        if ch:
            rows = []
            if ch.startswith("@"):
                rows.append([B(text=t(lang, "b_sub"), url=f"https://t.me/{ch[1:]}")])
            rows.append([B(text=t(lang, "check"), callback_data=f"p:{cat_}:{p}")])
            await screen(c, "sub", t(lang, "sub", ch=esc(ch)), KB(inline_keyboard=rows))
            return await c.answer()
    cond, args = "l.status='active'", []
    if cat_ != "all":
        cond += " and l.category=$1"
        args.append(cat_)
    n = await db.fetchval(f"select count(*) from listings l where {cond}", *args)
    if not n:
        await screen(c, "empty", t(lang, "empty"), home_kb())
        return await c.answer()
    p = max(0, min(int(p), n - 1))
    x = await db.fetchrow(
        f"select l.*, u.username from listings l join users u on u.id=l.owner where {cond} "
        f"order by l.updated desc, l.id offset {p} limit 1", *args)
    text = f"{t(lang, 'c_' + x['category'])}\n<b>{esc(x['title'])}</b>\n{esc(x['descr'])}"
    await log_event(c.from_user.id, "view")
    mine = x["owner"] == c.from_user.id
    rows = []
    if mine:
        text += f"\n\n<i>{t(lang, 'yours')}</i>"
    else:
        if x["username"]:
            url = f"https://t.me/{x['username']}"
        else:
            url = f"tg://user?id={x['owner']}"
            text += f"\n\n<i>{t(lang, 'hint')}</i>"
        rows += [[B(text=t(lang, "write"), url=url)], [B(text=t(lang, "report"), callback_data=f"rp:{x['id']}")]]
    nav = []
    if p > 0:
        nav.append(B(text="◀️", callback_data=f"p:{cat_}:{p - 1}"))
    nav.append(B(text=f"{p + 1}/{n}", callback_data="noop"))
    if p < n - 1:
        nav.append(B(text="▶️", callback_data=f"p:{cat_}:{p + 1}"))
    kb = KB(inline_keyboard=rows + [nav, [B(text="🏠", callback_data="menu")]])
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except Exception:
        try:
            await c.message.delete()
        except Exception:
            pass
        await c.message.answer(text, reply_markup=kb)
    await c.answer()


@r.callback_query(F.data.startswith("rp:"))
async def report(c: CallbackQuery, lang):
    lid = int(c.data[3:])
    await db.execute("insert into reports(listing_id,reporter) values($1,$2) on conflict do nothing", lid, c.from_user.id)
    await log_event(c.from_user.id, "report")
    cnt = await db.fetchval("select count(*) from reports where listing_id=$1", lid)
    if cnt >= int(await setting("report_threshold")):
        hid = await db.fetchval("update listings set status='hidden' where id=$1 and status='active' returning id", lid)
        if hid:
            for a in ADMIN_IDS:
                try:
                    await c.bot.send_message(a, f"⚠️ Анкету #{lid} приховано після {cnt} скарг. /admin → Скарги")
                except Exception:
                    pass
    await c.answer(t(lang, "thanks"), show_alert=True)


# --- my listings ---
@r.callback_query(F.data == "my")
async def my(c: CallbackQuery, lang):
    rows = await db.fetch("select * from listings where owner=$1 order by id desc limit 10", c.from_user.id)
    if not rows:
        await screen(c, "empty", t(lang, "empty"), home_kb())
    for x in rows:
        st = " " + t(lang, "hidden") if x["status"] == "hidden" else ""
        await c.message.answer(
            f"{t(lang, 'c_' + x['category'])}{st}\n<b>{esc(x['title'])}</b>\n{esc(x['descr'])}\n🔗 {esc(x['link'])}",
            reply_markup=KB(inline_keyboard=[[B(text=t(lang, "renew"), callback_data=f"lr:{x['id']}"),
                                              B(text=t(lang, "delete"), callback_data=f"ld:{x['id']}")]]))
    await c.answer()


@r.callback_query(F.data.startswith("lr:"))
async def renew(c: CallbackQuery, lang):
    await db.execute("update listings set updated=now(), warned=false where id=$1 and owner=$2",
                     int(c.data[3:]), c.from_user.id)
    await c.answer(t(lang, "done"), show_alert=True)


@r.callback_query(F.data.startswith("ld:"))
async def delete(c: CallbackQuery, lang):
    await db.execute("delete from listings where id=$1 and owner=$2", int(c.data[3:]), c.from_user.id)
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.answer(t(lang, "done"))


# ---------- ADMIN ROUTER (ukrainian only) ----------
ar = Router()
ar.message.filter(F.from_user.id.in_(ADMIN_IDS))
ar.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

SET_NAMES = {"ttl_days": "Термін життя анкети (дні)", "report_threshold": "Поріг скарг для приховування",
             "sub_required": "Обов'язкова підписка (1 - так, 0 - ні)", "sub_channel": "Канал (@username або ID)",
             "welcome_ru": "Привітання RU", "welcome_uk": "Привітання UK", "welcome_en": "Привітання EN"}


def admin_kb():
    rows = [("📊 Статистика", "adm:stats"), ("⚠️ Скарги", "adm:rep"), ("⚙️ Налаштування", "adm:set"),
            ("📣 Розсилка", "adm:bc")]
    return KB(inline_keyboard=[[B(text=a, callback_data=b)] for a, b in rows])


@ar.message(Command("admin"))
async def admin(m, lang, state: FSMContext):
    await state.clear()
    await m.answer("Адмін-панель.\nБан: /ban ID, розбан: /unban ID", reply_markup=admin_kb())


@ar.callback_query(F.data == "adm")
async def admin_cb(c: CallbackQuery, lang, state: FSMContext):
    await state.clear()
    await c.message.answer("Адмін-панель.\nБан: /ban ID, розбан: /unban ID", reply_markup=admin_kb())
    await c.answer()


@ar.message(Command("ban"))
async def ban(m, lang, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        return await m.answer("Використання: /ban ID")
    await ban_user(int(command.args))
    await m.answer("Забанено, анкети видалено.")


@ar.message(Command("unban"))
async def unban(m, lang, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        return await m.answer("Використання: /unban ID")
    await db.execute("update users set banned=false where id=$1", int(command.args))
    await m.answer("Розбанено.")


@ar.callback_query(F.data == "adm:stats")
async def stats(c: CallbackQuery):
    u = await db.fetchrow("""select count(*) total,
      count(*) filter (where banned) banned,
      count(*) filter (where created > now()-interval '1 day') n1,
      count(*) filter (where created > now()-interval '7 days') n7,
      count(*) filter (where created > now()-interval '30 days') n30,
      count(*) filter (where last_seen > now()-interval '1 day') a1,
      count(*) filter (where last_seen > now()-interval '7 days') a7
      from users""")
    l = await db.fetchrow("""select count(*) filter (where status='active') active,
      count(*) filter (where status='hidden') hidden,
      count(*) filter (where created > now()-interval '1 day') n1,
      count(*) filter (where created > now()-interval '7 days') n7
      from listings""")
    cats = await cat_counts()
    e = await db.fetchrow("""select
      count(*) filter (where kind='view' and created > now()-interval '1 day') v1,
      count(*) filter (where kind='view' and created > now()-interval '7 days') v7,
      count(*) filter (where kind='view') vall,
      count(distinct uid) filter (where kind='view' and created > now()-interval '7 days') vu7,
      count(*) filter (where kind='report' and created > now()-interval '7 days') r7
      from events""")
    days = await db.fetch("""select date_trunc('day', created) d, count(*) n from users
      where created > now()-interval '7 days' group by d""")
    today = datetime.now(timezone.utc).date()
    by_day = {x["d"].date(): x["n"] for x in days}
    vals = [by_day.get(today - timedelta(days=i), 0) for i in range(6, -1, -1)]
    mx = max(vals) or 1
    spark = "".join("▁▂▃▄▅▆▇█"[round(v / mx * 7)] for v in vals)
    text = (
        "📊 <b>Статистика</b>\n\n"
        "👥 <b>Користувачі</b>\n"
        f"Всього: {u['total']} · забанено: {u['banned']}\n"
        f"Нових: за добу +{u['n1']}, за тиждень +{u['n7']}, за місяць +{u['n30']}\n"
        f"Були в боті: за добу {u['a1']}, за тиждень {u['a7']}\n"
        f"Нові по днях (7 днів): {spark}\n\n"
        "📋 <b>Анкети</b>\n"
        f"Активних: {l['active']} · прихованих: {l['hidden']}\n"
        f"Нових: за добу +{l['n1']}, за тиждень +{l['n7']}\n"
        f"🤖 Боти: {cats.get('bot', 0)} · 📢 Канали: {cats.get('channel', 0)} · 📦 Інше: {cats.get('other', 0)}\n\n"
        "👁 <b>Каталог</b>\n"
        f"Переглядів: за добу {e['v1']}, за тиждень {e['v7']}, всього {e['vall']}\n"
        f"Унікальних глядачів за тиждень: {e['vu7']}\n\n"
        "⚠️ <b>Скарги</b>\n"
        f"За тиждень: {e['r7']}"
    )
    await c.message.answer(text, reply_markup=admin_kb())
    await c.answer()


async def next_report(target):
    x = await db.fetchrow("""select l.*, (select count(*) from reports where listing_id=l.id) rc
                             from listings l where status='hidden' order by id limit 1""")
    if not x:
        return await target.answer("Черга скарг порожня ✅", reply_markup=admin_kb())
    await target.answer(
        f"#{x['id']} | автор <code>{x['owner']}</code> | скарг: {x['rc']}\n<b>{esc(x['title'])}</b>\n"
        f"{esc(x['descr'])}\n🔗 {esc(x['link'])}",
        reply_markup=KB(inline_keyboard=[[B(text="✅ Повернути", callback_data=f"ao:{x['id']}"),
                                          B(text="🗑 Видалити", callback_data=f"ad:{x['id']}")],
                                         [B(text="🚫 Забанити автора", callback_data=f"ab:{x['id']}")]]))


@ar.callback_query(F.data == "adm:rep")
async def rep(c: CallbackQuery):
    await next_report(c.message)
    await c.answer()


@ar.callback_query(F.data.regexp(r"^a[odb]:\d+$"))
async def rep_action(c: CallbackQuery):
    act, lid = c.data[:2], int(c.data[3:])
    if act == "ao":
        await db.execute("update listings set status='active' where id=$1", lid)
        await db.execute("delete from reports where listing_id=$1", lid)
    elif act == "ad":
        await db.execute("delete from listings where id=$1", lid)
    else:
        owner = await db.fetchval("select owner from listings where id=$1", lid)
        if owner:
            await ban_user(owner)
    await c.answer("Готово")
    await next_report(c.message)


@ar.callback_query(F.data == "adm:set")
async def settings_menu(c: CallbackQuery):
    lines, rows = [], []
    for k, name in SET_NAMES.items():
        v = await setting(k)
        lines.append(f"<b>{name}</b>: {esc(v) or '—'}")
        rows.append([B(text=name, callback_data="as:" + k)])
    on = await setting("sub_required") == "1"
    rows.append([B(text="🔒 Вимкнути підписку" if on else "🔒 Увімкнути підписку", callback_data="adm:subtog")])
    await c.message.answer("\n".join(lines), reply_markup=KB(inline_keyboard=rows))
    await c.answer()


@ar.callback_query(F.data == "adm:subtog")
async def sub_toggle(c: CallbackQuery):
    new = "0" if await setting("sub_required") == "1" else "1"
    await set_setting("sub_required", new)
    await settings_menu(c)


@ar.callback_query(F.data.startswith("as:"))
async def setting_ask(c: CallbackQuery, state: FSMContext):
    k = c.data[3:]
    await state.set_state(Adm.val)
    await state.update_data(key=k)
    await c.message.answer(f"Надішліть нове значення для «{SET_NAMES[k]}» (або «-», щоб очистити):")
    await c.answer()


@ar.message(Adm.val, F.text)
async def setting_save(m, lang, state: FSMContext):
    k = (await state.get_data())["key"]
    v = "" if m.text.strip() == "-" else m.text.strip()
    if k in ("ttl_days", "report_threshold") and not v.isdigit():
        return await m.answer("Потрібне число.")
    if k == "sub_required" and v not in ("0", "1"):
        return await m.answer("Надішліть 0 або 1.")
    await set_setting(k, v)
    await state.clear()
    await m.answer("Збережено ✅", reply_markup=admin_kb())


@ar.callback_query(F.data == "adm:bc")
async def bc_ask(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.bc)
    await c.message.answer("Надішліть текст розсилки (HTML дозволено). /cancel — скасувати.")
    await c.answer()


@ar.message(Adm.bc, F.text)
async def bc_preview(m, lang, state: FSMContext):
    await state.update_data(text=m.html_text)
    await m.answer("Попередній перегляд:\n\n" + m.html_text,
                   reply_markup=KB(inline_keyboard=[[B(text="📣 Надіслати всім", callback_data="bc:go")],
                                                    [B(text="❌ Скасувати", callback_data="adm")]]))


@ar.callback_query(F.data == "bc:go")
async def bc_go(c: CallbackQuery, state: FSMContext):
    text = (await state.get_data()).get("text")
    await state.clear()
    if not text:
        return await c.answer("Немає тексту")
    await c.answer("Розсилаю...")
    ids = [x["id"] for x in await db.fetch("select id from users where not banned")]
    ok = 0
    for uid in ids:
        try:
            await c.bot.send_message(uid, text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await c.message.answer(f"Надіслано: {ok} з {len(ids)}", reply_markup=admin_kb())


# ---------- CLEANUP ----------
async def cleanup_loop(bot: Bot):
    while True:
        try:
            ttl = int(await setting("ttl_days"))
            rows = await db.fetch(
                """select l.id, l.owner, l.title, u.lang from listings l join users u on u.id=l.owner
                   where not l.warned and l.updated < now() - make_interval(days => $1)""", max(ttl - 1, 0))
            for x in rows:
                lang = x["lang"] if x["lang"] in LANGS else "ru"
                try:
                    await send_screen(bot, x["owner"], "expire", t(lang, "expiring", t=esc(x["title"])),
                                      KB(inline_keyboard=[[B(text=t(lang, "renew"), callback_data=f"lr:{x['id']}")]]))
                except Exception:
                    pass
                await db.execute("update listings set warned=true where id=$1", x["id"])
            await db.execute("delete from listings where updated < now() - make_interval(days => $1)", ttl)
        except Exception:
            logging.exception("cleanup failed")
        await asyncio.sleep(3600)


# ---------- STARTUP ----------
async def on_startup(bot: Bot):
    global db
    db = await asyncpg.create_pool(DSN, min_size=1, max_size=5, statement_cache_size=0)
    await db.execute(SCHEMA)
    await bot.set_webhook(f"{BASE_URL}/webhook", secret_token=SECRET, drop_pending_updates=True)
    bot._cleanup_task = asyncio.create_task(cleanup_loop(bot))


def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(Mw())
    dp.callback_query.outer_middleware(Mw())
    dp.include_router(ar)
    dp.include_router(r)
    dp.startup.register(on_startup)
    app = web.Application()

    async def health(_):
        return web.Response(text="ok")

    app.router.add_get("/health", health)
    SimpleRequestHandler(dp, bot, secret_token=SECRET).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
