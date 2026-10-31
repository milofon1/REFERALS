import asyncio, hashlib, html, logging, os, re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import asyncpg
from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton as B,
                           InlineKeyboardMarkup as KB, InputMediaPhoto, Message)
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
    "q_title": ("Шаг 2 из 4. Название сервиса, например MyBot (до 60 символов):",
                "Крок 2 з 4. Назва сервісу, наприклад MyBot (до 60 символів):",
                "Step 2 of 4. Service name, e.g. MyBot (up to 60 chars):"),
    "q_desc": ("Шаг 3 из 4. Описание (необязательно): что нужно сделать, например «подпишись на спонсоров, потом запусти фрикейс». До 300 символов.",
               "Крок 3 з 4. Опис (необов'язково): що треба зробити, наприклад «підпишись на спонсорів, потім запусти фрікейс». До 300 символів.",
               "Step 3 of 4. Description (optional): what to do, e.g. “subscribe to the sponsors, then spin the free case”. Up to 300 chars."),
    "q_link": ("Шаг 4 из 4. Реферальная ссылка. Подойдут: t.me/MyBot, https://..., @MyBot, tg://resolve?domain=MyBot",
               "Крок 4 з 4. Реферальне посилання. Підійдуть: t.me/MyBot, https://..., @MyBot, tg://resolve?domain=MyBot",
               "Step 4 of 4. Referral link. Accepted: t.me/MyBot, https://..., @MyBot, tg://resolve?domain=MyBot"),
    "bad": ("Не распознал ссылку. Примеры: t.me/MyBot, https://site.com/?ref=1, @MyBot, tg://resolve?domain=MyBot",
            "Не розпізнав посилання. Приклади: t.me/MyBot, https://site.com/?ref=1, @MyBot, tg://resolve?domain=MyBot",
            "Could not read the link. Examples: t.me/MyBot, https://site.com/?ref=1, @MyBot, tg://resolve?domain=MyBot"),
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
    "b_admin": ("🛠 Адмін-панель", "🛠 Адмін-панель", "🛠 Адмін-панель"),
    "b_cancel": ("❌ Отменить", "❌ Відмінити", "❌ Cancel"),
    "open_link": ("🔗 Открыть ссылку", "🔗 Відкрити посилання", "🔗 Open link"),
    "b_skip": ("⏭ Пропустить", "⏭ Пропустити", "⏭ Skip"),
    "need_text": ("Нужен текст. Напишите сообщением или нажмите «Отменить».", "Потрібен текст. Напишіть повідомленням або натисніть «Відмінити».",
                  "Text is needed. Write a message or tap “Cancel”."),
    "b_rating": ("🏆 Рейтинг", "🏆 Рейтинг", "🏆 Rating"),
    "b_rules": ("⚖️ Правила", "⚖️ Правила", "⚖️ Rules"),
    "b_back": ("◀️ Назад", "◀️ Назад", "◀️ Back"),
    "b_what": ("❓ Что такое рейтинг", "❓ Що таке рейтинг", "❓ What is rating"),
    "b_strikes": ("⚠️ Страйки и штрафы", "⚠️ Страйки і штрафи", "⚠️ Strikes and penalties"),
    "b_top": ("🥇 Топ", "🥇 Топ", "🥇 Top"),
    "b_accept": ("✅ Согласен", "✅ Погоджуюсь", "✅ I agree"),
    "claim": ("✅ Я сделал реферал", "✅ Я зробив реферал", "✅ I made a referral"),
    "yes_btn": ("✅ Подтвердить", "✅ Підтвердити", "✅ Confirm"),
    "no_btn": ("❌ Не было", "❌ Не було", "❌ Not done"),
    "rules_hidden": ("(скрыта: правила)", "(прихована: правила)", "(hidden: rules)"),
    "claim_ask": ("Пришлите скриншот регистрации по этой ссылке (фото). Без доказательства заявка не создаётся.",
                  "Надішліть скриншот реєстрації за цим посиланням (фото). Без доказу заявка не створюється.",
                  "Send a screenshot of your sign-up through this link (photo). Without proof the claim is not created."),
    "claim_need_photo": ("Нужен скриншот. Пришлите фото или /cancel.", "Потрібен скриншот. Надішліть фото або /cancel.",
                         "A screenshot is needed. Send a photo or /cancel."),
    "claim_sent": ("✅ Заявка отправлена автору. Если он не ответит за {d} дн., реферал засчитается автоматически.",
                   "✅ Заявку надіслано автору. Якщо він не відповість за {d} дн., реферал зарахується автоматично.",
                   "✅ Claim sent to the author. If there is no answer in {d} days, it counts automatically."),
    "claim_author": ("🔔 Кто-то утверждает, что сделал реферал на вашу анкету «{t}». Проверьте доказательство выше и подтвердите. Без ответа за {d} дн. засчитаем автоматически.",
                     "🔔 Хтось стверджує, що зробив реферал на вашу анкету «{t}». Перевірте доказ вище і підтвердіть. Без відповіді за {d} дн. зарахуємо автоматично.",
                     "🔔 Someone says they made a referral on your listing “{t}”. Check the proof above and confirm. No answer in {d} days and it counts automatically."),
    "claim_ok": ("✅ Реферал засчитан. Рейтинг и баланс обновлены.", "✅ Реферал зараховано. Рейтинг і баланс оновлено.",
                 "✅ Referral counted. Rating and balance updated."),
    "claim_fake": ("⚠️ Заявка признана фейковой: страйк +1, штраф {n} отработок, рейтинг −{n}. Отработки — подтверждённые рефералы для других. После них анкета снова видна.",
                   "⚠️ Заявку визнано фейковою: страйк +1, штраф {n} відпрацювань, рейтинг −{n}. Відпрацювання — підтверджені реферали для інших. Після них анкета знову видна.",
                   "⚠️ Claim marked as fake: strike +1, penalty {n} make-ups, rating −{n}. Make-ups are confirmed referrals for others. After them your listing is visible again."),
    "claim_disputed": ("⚖️ Автор возразил. Спор рассмотрит администратор, мы сообщим результат.",
                       "⚖️ Автор заперечив. Спір розгляне адміністратор, ми повідомимо результат.",
                       "⚖️ The author disputed it. An admin will review it and we will let you know."),
    "dispute_sent": ("Спор открыт. Администратор посмотрит доказательство.", "Спір відкрито. Адміністратор перегляне доказ.",
                     "Dispute opened. An admin will review the proof."),
    "b_support": ("🆘 Поддержка", "🆘 Підтримка", "🆘 Support"),
    "skip": ("⏭ Уже зарегистрирован", "⏭ Вже зареєстрований", "⏭ Already registered"),
    "sup_ask": ("Опишите вопрос или предложение одним сообщением (текст или фото). /cancel — отмена.",
                "Опишіть питання або пропозицію одним повідомленням (текст або фото). /cancel — скасувати.",
                "Describe your question or suggestion in one message (text or photo). /cancel to stop."),
    "sup_sent": ("✅ Спасибо! Ответим здесь, в этом чате.", "✅ Дякуємо! Відповімо тут, у цьому чаті.",
                 "✅ Thanks! We will reply here, in this chat."),
    "sup_reply": ("💬 <b>Ответ поддержки</b>\n\n{text}", "💬 <b>Відповідь підтримки</b>\n\n{text}",
                  "💬 <b>Support reply</b>\n\n{text}"),
    "lock_debt": ("🔒 Ваши анкеты скрыты из каталога: разница рефералов {d} (вам сделали больше, чем вы). Сделайте рефералы для других пользователей, и анкеты вернутся автоматически.",
                  "🔒 Ваші анкети приховано з каталогу: різниця рефералів {d} (вам зробили більше, ніж ви). Зробіть реферали для інших, і анкети повернуться автоматично.",
                  "🔒 Your listings are hidden from the catalog: referral gap {d} (you received more than you gave). Make referrals for others and they come back automatically."),
    "lock_penalty": ("🔒 Ваши анкеты скрыты из каталога: нужно отработать штраф ({p}). Сделайте подтверждённые рефералы для других, и анкеты вернутся автоматически.",
                     "🔒 Ваші анкети приховано з каталогу: потрібно відпрацювати штраф ({p}). Зробіть підтверджені реферали для інших, і анкети повернуться автоматично.",
                     "🔒 Your listings are hidden from the catalog: {p} make-ups owed. Make confirmed referrals for others and they come back automatically."),
    "unlock": ("✅ Ваши анкеты снова видны в каталоге.", "✅ Ваші анкети знову видно в каталозі.",
               "✅ Your listings are visible in the catalog again."),
    "claim_remind": ("⏰ Завтра заявка на «{t}» засчитается автоматически. Проверьте её и подтвердите или отклоните.",
                     "⏰ Завтра заявка на «{t}» зарахується автоматично. Перевірте її і підтвердіть або відхиліть.",
                     "⏰ Tomorrow the claim on “{t}” counts automatically. Check it and confirm or decline."),
    "dispute_limit": ("Лимит споров исчерпан. Подтвердите заявку или дождитесь автозачёта.",
                      "Ліміт спорів вичерпано. Підтвердіть заявку або дочекайтесь автозарахування.",
                      "Dispute limit reached. Confirm the claim or wait for the automatic count."),
    "claim_done": ("Заявка уже рассмотрена.", "Заявку вже розглянуто.", "This claim is already decided."),
    "claim_gone": ("Анкета больше не доступна.", "Анкета більше не доступна.", "This listing is no longer available."),
    "claim_age": ("Заявки можно подавать через {d} дн. после начала работы в боте.",
                  "Заявки можна подавати через {d} дн. після початку роботи в боті.",
                  "Claims open {d} days after you start using the bot."),
    "claim_limit": ("Слишком много открытых заявок. Дождитесь решения по ним.",
                    "Забагато відкритих заявок. Дочекайтесь рішень по них.", "Too many open claims. Wait for the decisions."),
    "claim_pair": ("Заявка этому автору уже открыта или недавно засчитана. Попробуйте позже.",
                   "Заявка цьому автору вже відкрита або нещодавно зарахована. Спробуйте пізніше.",
                   "A claim for this author is already open or was counted recently. Try later."),
    "rules_text": ("⚖️ <b>Правила взаимных рефералов</b>\n\n"
                   "1. Реферал — это реальная регистрация по ссылке автора. Фейки запрещены.\n"
                   "2. Заявка без скриншота не создаётся. Доказательство видят автор и администратор.\n"
                   "3. У автора {days} дн. на ответ. Без ответа реферал засчитывается автоматически.\n"
                   "4. Фейк, подтверждённый администратором: −{pen} к рейтингу, страйк и {pen} отработок. Пока штраф не отработан, анкета скрыта.\n"
                   "5. Разница между полученными и сделанными рефералами не больше {debt}. Иначе анкета скрыта, пока не выровняете.\n\n"
                   "Нажимая «Согласен», вы принимаете эти правила.",
                   "⚖️ <b>Правила взаємних рефералів</b>\n\n"
                   "1. Реферал — це реальна реєстрація за посиланням автора. Фейки заборонені.\n"
                   "2. Заявка без скриншота не створюється. Доказ бачать автор і адміністратор.\n"
                   "3. У автора {days} дн. на відповідь. Без відповіді реферал зараховується автоматично.\n"
                   "4. Фейк, підтверджений адміністратором: −{pen} до рейтингу, страйк і {pen} відпрацювань. Поки штраф не відпрацьовано, анкета прихована.\n"
                   "5. Різниця між отриманими і зробленими рефералами не більша за {debt}. Інакше анкета прихована, доки не вирівняєте.\n\n"
                   "Натискаючи «Погоджуюсь», ви приймаєте ці правила.",
                   "⚖️ <b>Mutual referral rules</b>\n\n"
                   "1. A referral is a real sign-up through the author's link. Fakes are not allowed.\n"
                   "2. A claim without a screenshot is not created. The author and an admin see the proof.\n"
                   "3. The author has {days} days to answer. No answer and the referral counts automatically.\n"
                   "4. A fake confirmed by an admin: −{pen} rating, a strike and {pen} make-ups. Until the penalty is worked off, your listing is hidden.\n"
                   "5. The gap between received and given referrals must not exceed {debt}. Otherwise your listing is hidden until you balance it.\n\n"
                   "By tapping “I agree” you accept these rules."),
    "rt_main": ("🏆 <b>Ваш рейтинг</b>\n\nРейтинг: <b>{s}</b>\nСделано рефералов: {g}\nПолучено рефералов: {r}\nДолг по балансу: {d}\nСтрайки: {st}\nОтработать штрафа: {p}",
                "🏆 <b>Ваш рейтинг</b>\n\nРейтинг: <b>{s}</b>\nЗроблено рефералів: {g}\nОтримано рефералів: {r}\nБорг за балансом: {d}\nСтрайки: {st}\nВідпрацювати штрафу: {p}",
                "🏆 <b>Your rating</b>\n\nRating: <b>{s}</b>\nReferrals given: {g}\nReferrals received: {r}\nBalance debt: {d}\nStrikes: {st}\nMake-ups owed: {p}"),
    "rt_what_text": ("❓ <b>Что такое рейтинг</b>\n\nРейтинг — ваша репутация в боте.\n+1 за каждый подтверждённый реферал, который вы сделали другим.\n−{pen} за фейк, подтверждённый администратором.\n\nРейтинг не прячет анкету, но показывает, насколько вы надёжны. Топ обновляется сам.",
                     "❓ <b>Що таке рейтинг</b>\n\nРейтинг — ваша репутація в боті.\n+1 за кожен підтверджений реферал, який ви зробили іншим.\n−{pen} за фейк, підтверджений адміністратором.\n\nРейтинг не ховає анкету, але показує, наскільки ви надійні. Топ оновлюється сам.",
                     "❓ <b>What is rating</b>\n\nRating is your reputation in the bot.\n+1 for each confirmed referral you made for someone else.\n−{pen} for a fake confirmed by an admin.\n\nRating does not hide your listing, but shows how reliable you are. The top updates automatically."),
    "rt_strikes_text": ("⚠️ <b>Страйки и штрафы</b>\n\nСтрайк — фейковая заявка, подтверждённая администратором.\nЗа каждый страйк: {pen} отработок и −{pen} к рейтингу.\nОтработка — подтверждённый реферал, который вы делаете для других. Каждая закрывает один пункт штрафа.\nПока штраф не отработан, анкета скрыта из каталога.",
                       "⚠️ <b>Страйки і штрафи</b>\n\nСтрайк — фейкова заявка, підтверджена адміністратором.\nЗа кожен страйк: {pen} відпрацювань і −{pen} до рейтингу.\nВідпрацювання — підтверджений реферал, який ви робите для інших. Кожне закриває один пункт штрафу.\nПоки штраф не відпрацьовано, анкета прихована з каталогу.",
                       "⚠️ <b>Strikes and penalties</b>\n\nA strike is a fake claim confirmed by an admin.\nEach strike: {pen} make-ups and −{pen} rating.\nA make-up is a confirmed referral you make for others. Each one clears one point of the penalty.\nWhile the penalty is not worked off, your listing is hidden from the catalog."),
    "rt_top_title": ("🥇 <b>Топ рейтинга</b>\n\n", "🥇 <b>Топ рейтингу</b>\n\n", "🥇 <b>Rating top</b>\n\n"),
    "rt_top_empty": ("Пока пусто.", "Поки порожньо.", "Nothing here yet."),
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
            "welcome_ru": "", "welcome_uk": "", "welcome_en": "",
            "claim_days": "3", "open_claims_max": "3", "pair_cooldown_days": "30",
            "debt_limit": "2", "penalty_per_strike": "3", "disputes_per_10": "5", "catalog_link": "0"}
NUM_KEYS = ("ttl_days", "report_threshold", "claim_days", "open_claims_max",
            "pair_cooldown_days", "debt_limit", "penalty_per_strike", "disputes_per_10", "catalog_link")


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
alter table users add column if not exists rules_ok boolean not null default false;
alter table users add column if not exists given int not null default 0;
alter table users add column if not exists received int not null default 0;
alter table users add column if not exists score int not null default 0;
alter table users add column if not exists strikes int not null default 0;
alter table users add column if not exists penalty_due int not null default 0;
alter table listings add column if not exists rules_hidden boolean not null default false;
create table if not exists claims(id bigserial primary key, listing_id int not null references listings(id) on delete cascade,
  giver bigint not null, receiver bigint not null, proof text not null, status text not null default 'pending',
  created timestamptz not null default now(), decided timestamptz, disputed_at timestamptz);
create index if not exists claims_giver on claims(giver, status);
create index if not exists claims_pair on claims(giver, receiver, status);
alter table users add column if not exists locked boolean not null default false;
alter table claims add column if not exists reminded boolean not null default false;
create table if not exists skips(uid bigint not null, listing_id int not null references listings(id) on delete cascade,
  primary key(uid, listing_id));
create table if not exists support_tickets(id bigserial primary key, uid bigint not null, mid bigint,
  status text not null default 'open', created timestamptz not null default now());
"""


async def ban_user(uid):
    await db.execute("insert into users(id, banned) values($1, true) on conflict(id) do update set banned=true", uid)
    await db.execute("delete from listings where owner=$1", uid)


async def log_event(uid, kind):
    await db.execute("insert into events(uid, kind) values($1, $2)", uid, kind)


async def cat_counts():
    rows = await db.fetch("select category, count(*) n from listings where status='active' and not rules_hidden group by category")
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


# ---------- RATING, RULES AND CLAIMS (helpers) ----------
async def user_lang(uid):
    lang = await db.fetchval("select lang from users where id=$1", uid)
    return lang if lang in LANGS else "ru"


async def notify(bot, uid, key, **kw):
    try:
        await bot.send_message(uid, t(await user_lang(uid), key, **kw))
    except Exception:
        pass


async def refresh_rules(uid, bot=None):
    """Hides the owner's listings while the balance gap or an unpaid penalty is too high, and tells the owner once."""
    u = await db.fetchrow("select given, received, penalty_due, locked from users where id=$1", uid)
    if not u:
        return
    debt = u["received"] - u["given"]
    locked = debt >= int(await setting("debt_limit")) or u["penalty_due"] > 0
    await db.execute("update listings set rules_hidden=$2 where owner=$1", uid, locked)
    if locked == u["locked"]:
        return
    await db.execute("update users set locked=$2 where id=$1", uid, locked)
    has_listings = await db.fetchval("select exists(select 1 from listings where owner=$1)", uid)
    if bot is None or not has_listings:
        return
    if not locked:
        await notify(bot, uid, "unlock")
    elif u["penalty_due"] > 0:
        await notify(bot, uid, "lock_penalty", p=u["penalty_due"])
    else:
        await notify(bot, uid, "lock_debt", d=debt)


async def claim_block_reason(uid, owner, lang):
    open_n = await db.fetchval("select count(*) from claims where giver=$1 and status in ('pending','disputed')", uid)
    if open_n >= int(await setting("open_claims_max")):
        return t(lang, "claim_limit")
    busy = await db.fetchval(
        """select 1 from claims where giver=$1 and receiver=$2 and (status in ('pending','disputed')
           or (status='confirmed' and decided > now() - make_interval(days => $3))) limit 1""",
        uid, owner, int(await setting("pair_cooldown_days")))
    return t(lang, "claim_pair") if busy else None


async def dispute_limit_hit(uid):
    """Ratio cap: at most disputes_per_10 out of every 10 claims received (rounded up), counting this one."""
    received = await db.fetchval("select count(*) from claims where receiver=$1", uid)
    disputed = await db.fetchval("select count(*) from claims where receiver=$1 and disputed_at is not null", uid)
    per10 = int(await setting("disputes_per_10"))
    allowed = -(-received * per10 // 10)
    return disputed + 1 > allowed



async def claim_card(cid):
    return await db.fetchrow(
        """select c.id, c.giver, c.receiver, c.proof, l.id lid, l.title
           from claims c join listings l on l.id=c.listing_id where c.id=$1""", cid)


async def apply_confirmed(bot, cid):
    """Counts a referral as done. Guarded by status, so a double tap or a race cannot count it twice."""
    row = await db.fetchrow("""update claims set status='confirmed', decided=now()
        where id=$1 and status in ('pending','disputed') returning giver, receiver""", cid)
    if not row:
        return False
    g, rc = row["giver"], row["receiver"]
    await db.execute("update users set given=given+1, score=score+1 where id=$1", g)
    await db.execute("update users set received=received+1 where id=$1", rc)
    await db.execute("update users set penalty_due=penalty_due-1 where id=$1 and penalty_due>0", g)
    await refresh_rules(g, bot)
    await refresh_rules(rc, bot)
    await notify(bot, g, "claim_ok")
    await notify(bot, rc, "claim_ok")
    return True


async def reject_claim(bot, cid):
    row = await db.fetchrow("""update claims set status='rejected', decided=now()
        where id=$1 and status in ('pending','disputed') returning giver""", cid)
    if not row:
        return False
    per = int(await setting("penalty_per_strike"))
    await db.execute("update users set strikes=strikes+1, penalty_due=penalty_due+$2, score=score-$2 where id=$1",
                     row["giver"], per)
    await refresh_rules(row["giver"], bot)
    await notify(bot, row["giver"], "claim_fake", n=per)
    return True


async def send_claim_to_author(bot, cid):
    card = await claim_card(cid)
    lang = await user_lang(card["receiver"])
    kb = KB(inline_keyboard=[[B(text=t(lang, "yes_btn"), callback_data=f"cf:{cid}"),
                              B(text=t(lang, "no_btn"), callback_data=f"cd:{cid}")]])
    try:
        await bot.send_photo(card["receiver"], card["proof"],
                             caption=t(lang, "claim_author", t=esc(card["title"]), d=await setting("claim_days")),
                             reply_markup=kb)
    except Exception:
        logging.exception("could not send the claim to the author")


def dispute_caption(card):
    return (f"⚖️ Спір #{card['id']}\nЗаявник: <code>{card['giver']}</code>\nАвтор: <code>{card['receiver']}</code>\n"
            f"Анкета #{card['lid']} «{esc(card['title'])}»\nДоказ заявника — вище.")


def dispute_kb(cid):
    return KB(inline_keyboard=[[B(text="✅ Зарахувати", callback_data=f"dv:{cid}:ok"),
                                B(text="❌ Фейк", callback_data=f"dv:{cid}:fake")]])


async def notify_admins_dispute(bot, cid):
    card = await claim_card(cid)
    for a in ADMIN_IDS:
        try:
            await bot.send_photo(a, card["proof"], caption=dispute_caption(card), reply_markup=dispute_kb(cid))
        except Exception:
            logging.exception("could not send the dispute to admin")


async def claims_expiry(bot):
    """Reminds the author a day before, auto-counts unanswered claims, closes disputes nobody decided within 7 days."""
    try:
        days = int(await setting("claim_days"))
        if days > 1:
            for x in await db.fetch("""select c.id, c.receiver, l.title from claims c join listings l on l.id=c.listing_id
                    where c.status='pending' and not c.reminded and c.created < now() - make_interval(days => $1)""", days - 1):
                await db.execute("update claims set reminded=true where id=$1", x["id"])
                await notify(bot, x["receiver"], "claim_remind", t=esc(x["title"]))
        for x in await db.fetch("select id from claims where status='pending' and created < now() - make_interval(days => $1)", days):
            await apply_confirmed(bot, x["id"])
        await db.execute("""update claims set status='expired', decided=now()
            where status='disputed' and disputed_at < now() - interval '7 days'""")
    except Exception:
        logging.exception("claims expiry failed")


async def rules_caption(lang):
    return t(lang, "rules_text", days=await setting("claim_days"), pen=await setting("penalty_per_strike"),
             debt=await setting("debt_limit"))


async def rating_text(uid, lang):
    u = await db.fetchrow("select score, strikes, given, received, penalty_due from users where id=$1", uid)
    return t(lang, "rt_main", s=u["score"], g=u["given"], r=u["received"], d=max(0, u["received"] - u["given"]),
             st=u["strikes"], p=u["penalty_due"])


def rules_kb(lang, accept=True):
    rows = [[B(text=t(lang, "b_accept"), callback_data="rules:ok")]] if accept else []
    rows.append([B(text="🏠", callback_data="menu")])
    return KB(inline_keyboard=rows)


def rating_kb(lang):
    return KB(inline_keyboard=[
        [B(text=t(lang, "b_what"), callback_data="rt:what"), B(text=t(lang, "b_strikes"), callback_data="rt:strikes")],
        [B(text=t(lang, "b_top"), callback_data="rt:top"), B(text=t(lang, "b_rules"), callback_data="rules:view")],
        [B(text="🏠", callback_data="menu")]])


def sub_kb(lang, back="rt"):
    return KB(inline_keyboard=[[B(text=t(lang, "b_back"), callback_data=back)], [B(text="🏠", callback_data="menu")]])


# ---------- KEYBOARDS ----------
def lang_kb():
    return KB(inline_keyboard=[[B(text="🇷🇺 Русский", callback_data="sl:ru"),
                                B(text="🇺🇦 Українська", callback_data="sl:uk"),
                                B(text="🇬🇧 English", callback_data="sl:en")]])


def menu_kb(lang, is_admin=False):
    def b(k, cb):
        return B(text=t(lang, k), callback_data=cb)
    rows = [[b("b_cat", "cat")], [b("b_new", "new"), b("b_my", "my")], [b("b_rating", "rt"), b("b_lang", "lang")], [b("b_support", "sup")]]
    if is_admin:
        rows.append([b("b_admin", "adm")])
    return KB(inline_keyboard=rows)


# ---------- ILLUSTRATIONS ----------
ROOT_DIR = Path(__file__).parent
IMG_DIRS = [ROOT_DIR]  # all images sit next to main.py
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
    """Updates the bot's current message in place: one API call, nothing deleted.
    Switching between photo and text can't be done in place, so those cases send a new message."""
    if not isinstance(event, CallbackQuery):
        return await send_screen(event.bot, event.chat.id, name, text, kb)
    msg = event.message
    path = find_img(name)
    fits = len(text) <= 1024
    try:
        if path and fits and msg.photo:
            fid = _file_ids.get(name)
            res = await msg.edit_media(InputMediaPhoto(media=fid or FSInputFile(path), caption=text), reply_markup=kb)
            if not fid:
                _file_ids[name] = res.photo[-1].file_id
            return res
        if not path and fits and msg.photo:
            return await msg.edit_caption(caption=text, reply_markup=kb)
        if not path and not msg.photo:
            return await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return msg
        logging.warning("in-place edit failed, sending new message: %s", e)
    res = await send_screen(event.bot, msg.chat.id, name, text, kb)
    try:
        await msg.delete()
    except Exception:
        pass
    return res


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
    reply = State()


# ---------- USER ROUTER ----------
r = Router()


async def send_menu(target, lang, text=None, img="welcome"):
    await screen(target, img, text or t(lang, "menu"), menu_kb(lang, target.from_user.id in ADMIN_IDS))


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
    await c.message.answer(t(lang, "q_title"), reply_markup=cancel_kb(lang))
    await c.answer()


@r.message(New.title, F.text)
async def new_title(m: Message, lang, state: FSMContext):
    title = m.text.strip()[:60]
    if not title:
        return await m.answer(t(lang, "q_title"), reply_markup=cancel_kb(lang))
    await state.update_data(title=title)
    await state.set_state(New.desc)
    await m.answer(t(lang, "q_desc"), reply_markup=skip_cancel_kb(lang))


@r.message(New.desc, F.text)
async def new_desc(m: Message, lang, state: FSMContext):
    await state.update_data(desc=m.text.strip()[:300])
    await state.set_state(New.link)
    await m.answer(t(lang, "q_link"), reply_markup=cancel_kb(lang))


@r.callback_query(F.data == "nskip", StateFilter(New.desc))
async def new_desc_skip(c: CallbackQuery, lang, state: FSMContext):
    await state.update_data(desc="")
    await state.set_state(New.link)
    await c.message.answer(t(lang, "q_link"), reply_markup=cancel_kb(lang))
    await c.answer()


@r.message(New.link, F.text)
async def new_link(m: Message, lang, state: FSMContext):
    link = normalize_link(m.text)
    if not link:
        return await m.answer(t(lang, "bad"), reply_markup=cancel_kb(lang))
    d = await state.get_data()
    await db.execute("insert into listings(owner,category,title,descr,link) values($1,$2,$3,$4,$5)",
                     m.from_user.id, d["cat"], d["title"], d.get("desc", ""), link)
    await log_event(m.from_user.id, "create")
    await refresh_rules(m.from_user.id, m.bot)
    await state.clear()
    await send_menu(m, lang, t(lang, "saved"), "saved")


@r.message(StateFilter(New.title, New.desc, New.link))
async def new_not_text(m: Message, lang):
    await m.answer(t(lang, "need_text"), reply_markup=cancel_kb(lang))


# --- catalog ---
@r.callback_query(F.data == "cat")
async def cat(c: CallbackQuery, lang):
    if not await db.fetchval("select rules_ok from users where id=$1", c.from_user.id):
        await screen(c, "rules", await rules_caption(lang), rules_kb(lang))
        return await c.answer()
    counts = await cat_counts()
    await screen(c, "catalog", t(lang, "cat_head"), cat_kb(lang, lambda k: f"p:{k}:0", counts))
    await c.answer()


async def show_page(c, lang, cat_, p):
    cond, args = "l.status='active' and not l.rules_hidden", []
    if cat_ != "all":
        args.append(cat_)
        cond += f" and l.category=${len(args)}"
    args.append(c.from_user.id)
    cond += f" and not exists (select 1 from skips s where s.uid=${len(args)} and s.listing_id=l.id)"
    n = await db.fetchval(f"select count(*) from listings l where {cond}", *args)
    if not n:
        await screen(c, "empty", t(lang, "empty"), home_kb())
        return await c.answer()
    p = max(0, min(int(p), n - 1))
    x = await db.fetchrow(
        f"select l.*, u.username from listings l join users u on u.id=l.owner where {cond} "
        f"order by l.updated desc, l.id offset {p} limit 1", *args)
    await log_event(c.from_user.id, "view")
    text = f"{t(lang, 'c_' + x['category'])}\n<b>{esc(x['title'])}</b>" + (f"\n{esc(x['descr'])}" if x["descr"] else "")
    rows = []
    if x["owner"] == c.from_user.id:
        text += f"\n\n<i>{t(lang, 'yours')}</i>"
    else:
        if x["username"]:
            url = f"https://t.me/{x['username']}"
        else:
            url = f"tg://user?id={x['owner']}"
            text += f"\n\n<i>{t(lang, 'hint')}</i>"
        if await setting("catalog_link") == "1":
            rows.append([B(text=t(lang, "open_link"), url=x["link"])])
        rows += [[B(text=t(lang, "claim"), callback_data=f"cl:{x['id']}")],
                 [B(text=t(lang, "skip"), callback_data=f"sk:{x['id']}:{cat_}:{p}")],
                 [B(text=t(lang, "write"), url=url)],
                 [B(text=t(lang, "report"), callback_data=f"rp:{x['id']}")]]
    nav = []
    if p > 0:
        nav.append(B(text="◀️", callback_data=f"p:{cat_}:{p - 1}"))
    nav.append(B(text=f"{p + 1}/{n}", callback_data="noop"))
    if p < n - 1:
        nav.append(B(text="▶️", callback_data=f"p:{cat_}:{p + 1}"))
    kb = KB(inline_keyboard=rows + [nav, [B(text="🏠", callback_data="menu")]])
    await screen(c, "browse", text, kb)
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
    await show_page(c, lang, cat_, int(p))


@r.callback_query(F.data.startswith("sk:"))
async def skip_listing(c: CallbackQuery, lang):
    _, lid, cat_, p = c.data.split(":")
    await db.execute("insert into skips(uid, listing_id) values($1,$2) on conflict do nothing",
                     c.from_user.id, int(lid))
    await show_page(c, lang, cat_, int(p))


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
        if x["rules_hidden"]:
            st += " " + t(lang, "rules_hidden")
        await c.message.answer(
            f"{t(lang, 'c_' + x['category'])}{st}\n<b>{esc(x['title'])}</b>\n{esc(x['descr'])}\n🔗 <a href='{esc(x['link'])}'>{esc(x['link'])}</a>",
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


# --- shared inputs: cancel and skip buttons ---
def cancel_kb(lang):
    return KB(inline_keyboard=[[B(text=t(lang, "b_cancel"), callback_data="cancel")]])


def skip_cancel_kb(lang):
    return KB(inline_keyboard=[[B(text=t(lang, "b_skip"), callback_data="nskip")],
                               [B(text=t(lang, "b_cancel"), callback_data="cancel")]])


@r.callback_query(F.data == "cancel")
async def cancel_cb(c: CallbackQuery, lang, state: FSMContext):
    await state.clear()
    await send_menu(c, lang)
    await c.answer()


def normalize_link(raw):
    """Accepts t.me/..., telegram.me/..., https://..., http://..., bare domains, @username and tg:// deep links."""
    s = (raw or "").strip()
    if not s or len(s) > 290 or re.search(r"\s", s):
        return None
    if re.fullmatch(r"@[A-Za-z0-9_]{4,32}", s):
        return f"https://t.me/{s[1:]}"
    if re.fullmatch(r"tg://\S{3,250}", s, re.IGNORECASE):
        return s
    if not re.match(r"https?://", s, re.IGNORECASE):
        if not re.match(r"[\w.-]+\.[a-z]{2,}(/\S*)?$", s, re.IGNORECASE):
            return None
        s = "https://" + s
    p = urlparse(s)
    if p.scheme not in ("http", "https") or "." not in p.netloc:
        return None
    return urlunparse(p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower()))


# --- rules, rating and referral claims ---
class Claim(StatesGroup):
    proof = State()


@r.callback_query(F.data == "rules:ok")
async def rules_accept(c: CallbackQuery, lang):
    await db.execute("update users set rules_ok=true where id=$1", c.from_user.id)
    await cat(c, lang)


@r.callback_query(F.data == "rules:view")
async def rules_view(c: CallbackQuery, lang):
    await screen(c, "rules", await rules_caption(lang), rules_kb(lang, accept=False))
    await c.answer()


@r.callback_query(F.data == "rt")
async def rating_main(c: CallbackQuery, lang):
    await screen(c, "rating", await rating_text(c.from_user.id, lang), rating_kb(lang))
    await c.answer()


@r.callback_query(F.data == "rt:what")
async def rating_what(c: CallbackQuery, lang):
    await screen(c, "rating", t(lang, "rt_what_text", pen=await setting("penalty_per_strike")), sub_kb(lang))
    await c.answer()


@r.callback_query(F.data == "rt:strikes")
async def rating_strikes(c: CallbackQuery, lang):
    await screen(c, "rating", t(lang, "rt_strikes_text", pen=await setting("penalty_per_strike")), sub_kb(lang))
    await c.answer()


@r.callback_query(F.data == "rt:top")
async def rating_top(c: CallbackQuery, lang):
    rows = await db.fetch("select username, score from users where score>0 and not banned order by score desc, id limit 10")
    body = "\n".join(f"{i}. {('@' + esc(x['username'])) if x['username'] else '—'} · {x['score']}"
                     for i, x in enumerate(rows, 1))
    await screen(c, "rating", t(lang, "rt_top_title") + (body if rows else t(lang, "rt_top_empty")), sub_kb(lang))
    await c.answer()


@r.callback_query(F.data.startswith("cl:"))
async def claim_start(c: CallbackQuery, lang, state: FSMContext):
    lid = int(c.data[3:])
    x = await db.fetchrow("select id, owner from listings where id=$1 and status='active' and not rules_hidden", lid)
    if not x:
        return await c.answer(t(lang, "claim_gone"), show_alert=True)
    if x["owner"] == c.from_user.id:
        return await c.answer()
    reason = await claim_block_reason(c.from_user.id, x["owner"], lang)
    if reason:
        return await c.answer(reason, show_alert=True)
    await state.set_state(Claim.proof)
    await state.update_data(lid=lid)
    await send_screen(c.bot, c.message.chat.id, "proof", t(lang, "claim_ask"), cancel_kb(lang))
    await c.answer()


@r.message(Claim.proof, F.photo)
async def claim_proof(m: Message, lang, state: FSMContext):
    lid = (await state.get_data())["lid"]
    await state.clear()
    x = await db.fetchrow("select id, owner from listings where id=$1 and status='active' and not rules_hidden", lid)
    if not x:
        return await m.answer(t(lang, "claim_gone"))
    reason = await claim_block_reason(m.from_user.id, x["owner"], lang)
    if reason:
        return await m.answer(reason)
    cid = await db.fetchval(
        "insert into claims(listing_id, giver, receiver, proof) values($1,$2,$3,$4) returning id",
        lid, m.from_user.id, x["owner"], m.photo[-1].file_id)
    await m.answer(t(lang, "claim_sent", d=await setting("claim_days")))
    await send_claim_to_author(m.bot, cid)


@r.message(Claim.proof)
async def claim_proof_wrong(m: Message, lang):
    await m.answer(t(lang, "claim_need_photo"), reply_markup=cancel_kb(lang))


@r.callback_query(F.data.regexp(r"^c[fd]:\d+$"))
async def claim_answer(c: CallbackQuery, lang):
    cid = int(c.data[3:])
    row = await db.fetchrow("select status, receiver, giver from claims where id=$1", cid)
    if not row or row["receiver"] != c.from_user.id or row["status"] != "pending":
        return await c.answer(t(lang, "claim_done"), show_alert=True)
    if c.data.startswith("cf:"):
        await apply_confirmed(c.bot, cid)
        await c.answer(t(lang, "done"))
    elif await dispute_limit_hit(c.from_user.id):
        return await c.answer(t(lang, "dispute_limit"), show_alert=True)
    else:
        await db.execute("update claims set status='disputed', disputed_at=now() where id=$1 and status='pending'", cid)
        await send_screen(c.bot, row["giver"], "dispute", t(await user_lang(row["giver"]), "claim_disputed"))
        await notify_admins_dispute(c.bot, cid)
        await c.answer(t(lang, "dispute_sent"), show_alert=True)
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# --- support ---
class Support(StatesGroup):
    write = State()


@r.callback_query(F.data == "sup")
async def support_start(c: CallbackQuery, lang, state: FSMContext):
    await state.set_state(Support.write)
    await send_screen(c.bot, c.message.chat.id, "support", t(lang, "sup_ask"), cancel_kb(lang))
    await c.answer()


@r.message(Support.write)
async def support_msg(m: Message, lang, state: FSMContext):
    await state.clear()
    tid = await db.fetchval("insert into support_tickets(uid, mid) values($1,$2) returning id",
                            m.from_user.id, m.message_id)
    who = f"<code>{m.from_user.id}</code> @{esc(m.from_user.username or '—')}"
    for a in ADMIN_IDS:
        try:
            await m.forward(a)
            await m.bot.send_message(a, f"🆘 Звернення #{tid}\nВід: {who}",
                                     reply_markup=KB(inline_keyboard=[[B(text="✍️ Відповісти", callback_data=f"sr:{tid}")]]))
        except Exception:
            logging.exception("could not forward support ticket to admin")
    await send_screen(m.bot, m.chat.id, "support", t(lang, "sup_sent"), home_kb())


# ---------- ADMIN ROUTER (ukrainian only) ----------
ar = Router()
ar.message.filter(F.from_user.id.in_(ADMIN_IDS))
ar.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

SET_NAMES = {"ttl_days": "Термін життя анкети (дні)", "report_threshold": "Поріг скарг для приховування",
             "sub_required": "Обов'язкова підписка (1 - так, 0 - ні)", "sub_channel": "Канал (@username або ID)",
             "claim_days": "Днів на відповідь автора (автозарахування)",
             "open_claims_max": "Макс. відкритих заявок на користувача",
             "pair_cooldown_days": "Пауза між парами (днів)",
             "debt_limit": "Поріг боргу (анкета ховається)",
             "penalty_per_strike": "Штраф за фейк (відпрацювань і рейтингу)",
             "disputes_per_10": "Спорів на кожні 10 заявок до вас (5 = половина)",
             "catalog_link": "Кнопка з посиланням у каталозі (1 - так, 0 - ні)",
             "welcome_ru": "Привітання RU", "welcome_uk": "Привітання UK", "welcome_en": "Привітання EN"}


def admin_kb():
    rows = [("📊 Статистика", "adm:stats"), ("⚖️ Спори", "adm:disp"), ("💬 Звернення", "adm:sup"), ("⚠️ Скарги", "adm:rep"),
            ("⚙️ Налаштування", "adm:set"), ("📣 Розсилка", "adm:bc")]
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


# ---------- STATS (admin, sent as an image) ----------
ST_BG, ST_PINK, ST_GREY, ST_WHITE = (17, 17, 17), (255, 42, 138), (150, 150, 150), (245, 245, 245)
FONT_REG = ROOT_DIR / "DejaVuSans.ttf"
FONT_BOLD = ROOT_DIR / "DejaVuSans-Bold.ttf"


async def stats_data():
    u = await db.fetchrow("""select count(*) total,
      count(*) filter (where banned) banned,
      count(*) filter (where created > now()-interval '1 day') n1,
      count(*) filter (where created > now()-interval '7 days') n7,
      count(*) filter (where created > now()-interval '30 days') n30,
      count(*) filter (where last_seen > now()-interval '1 day') a1,
      count(*) filter (where last_seen > now()-interval '7 days') a7
      from users""")
    l = await db.fetchrow("""select count(*) filter (where status='active') active,
      count(*) filter (where status='hidden') hidden
      from listings""")
    e = await db.fetchrow("""select
      count(*) filter (where kind='view' and created > now()-interval '1 day') v1,
      count(*) filter (where kind='view' and created > now()-interval '7 days') v7,
      count(*) filter (where kind='view') vall,
      count(*) filter (where kind='report' and created > now()-interval '7 days') r7
      from events""")
    days = await db.fetch("""select date_trunc('day', created) d, count(*) n from users
      where created > now()-interval '7 days' group by d""")
    today = datetime.now(timezone.utc).date()
    by_day = {x["d"].date(): x["n"] for x in days}
    week = [today - timedelta(days=i) for i in range(6, -1, -1)]
    return {
        "total": u["total"], "banned": u["banned"], "n1": u["n1"], "n7": u["n7"], "n30": u["n30"],
        "a1": u["a1"], "a7": u["a7"], "active": l["active"], "hidden": l["hidden"],
        "l7": await db.fetchval("select count(*) from listings where created > now()-interval '7 days'"),
        "cats": await cat_counts(),
        "v1": e["v1"], "v7": e["v7"], "vall": e["vall"], "r7": e["r7"],
        "days": [(d.strftime("%d.%m"), by_day.get(d, 0)) for d in week],
    }


def stats_text(s):
    vals = [v for _, v in s["days"]]
    mx = max(vals) or 1
    spark = "".join("▁▂▃▄▅▆▇█"[round(v / mx * 7)] for v in vals)
    cats = s["cats"]
    return (
        "📊 <b>Статистика</b>\n\n"
        "👥 <b>Користувачі</b>\n"
        f"Всього: {s['total']} · забанено: {s['banned']}\n"
        f"Нових: за добу +{s['n1']}, за тиждень +{s['n7']}, за місяць +{s['n30']}\n"
        f"Були в боті: за добу {s['a1']}, за тиждень {s['a7']}\n"
        f"Нові по днях: {spark}\n\n"
        "📋 <b>Анкети</b>\n"
        f"Активних: {s['active']} · прихованих: {s['hidden']}\n"
        f"Нових: за тиждень +{s['l7']}\n"
        f"🤖 Боти: {cats.get('bot', 0)} · 📢 Канали: {cats.get('channel', 0)} · 📦 Інше: {cats.get('other', 0)}\n\n"
        "👁 <b>Каталог</b>\n"
        f"Переглядів: за добу {s['v1']}, за тиждень {s['v7']}, всього {s['vall']}\n\n"
        "⚠️ <b>Скарги</b>\n"
        f"За тиждень: {s['r7']}"
    )


def render_stats(s):
    """Draws the statistics dashboard as a PNG (Pillow + bundled DejaVu fonts)."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont
    W, H = 1280, 720
    img = Image.new("RGB", (W, H), ST_BG)
    d = ImageDraw.Draw(img)
    f_title = ImageFont.truetype(str(FONT_BOLD), 52)
    f_big = ImageFont.truetype(str(FONT_BOLD), 66)
    f_lbl = ImageFont.truetype(str(FONT_REG), 26)
    f_bold = ImageFont.truetype(str(FONT_BOLD), 26)
    f_small = ImageFont.truetype(str(FONT_REG), 22)

    d.text((60, 36), "Статистика бота", font=f_title, fill=ST_PINK)

    cards = [
        ("Користувачі", s["total"], f"+{s['n7']} за тиждень"),
        ("Були в боті", s["a7"], f"за добу: {s['a1']}"),
        ("Активних анкет", s["active"], f"+{s['l7']} за тиждень"),
        ("Переглядів", s["v7"], f"за добу: {s['v1']}"),
    ]
    cw, ch, gap, y = 280, 170, 13, 120
    for i, (lbl, val, sub) in enumerate(cards):
        x = 60 + i * (cw + gap)
        d.rounded_rectangle((x, y, x + cw, y + ch), radius=24, outline=ST_PINK, width=4)
        d.text((x + 24, y + 22), lbl, font=f_lbl, fill=ST_GREY)
        d.text((x + 24, y + 58), str(val), font=f_big, fill=ST_WHITE)
        d.text((x + 24, y + 134), sub, font=f_small, fill=ST_GREY)

    d.text((60, 330), "Нові користувачі за 7 днів", font=f_bold, fill=ST_WHITE)
    base, top = 640, 400
    vals = [v for _, v in s["days"]]
    mx = max(vals) or 1
    bw, bgap = 78, 26
    for i, (lbl, v) in enumerate(s["days"]):
        x = 60 + i * (bw + bgap)
        h = max(6, round(v / mx * (base - top))) if v else 6
        d.rounded_rectangle((x, base - h, x + bw, base), radius=10, fill=ST_PINK if v else (60, 60, 60))
        d.text((x + bw / 2, base - h - 30), str(v), font=f_bold, fill=ST_WHITE, anchor="mm")
        d.text((x + bw / 2, base + 26), lbl, font=f_small, fill=ST_GREY, anchor="mm")

    cats = s["cats"]
    details = [
        ("Нових за добу", f"+{s['n1']}"),
        ("Нових за місяць", f"+{s['n30']}"),
        ("Забанено", s["banned"]),
        ("Анкети: боти", cats.get("bot", 0)),
        ("Анкети: канали", cats.get("channel", 0)),
        ("Анкети: інше", cats.get("other", 0)),
        ("Прихованих анкет", s["hidden"]),
        ("Скарг за тиждень", s["r7"]),
    ]
    for i, (lbl, val) in enumerate(details):
        yy = 380 + i * 36
        d.text((860, yy), lbl, font=f_lbl, fill=ST_GREY)
        d.text((1220, yy), str(val), font=f_bold, fill=ST_PINK, anchor="ra")

    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@ar.callback_query(F.data == "adm:stats")
async def stats(c: CallbackQuery):
    s = await stats_data()
    try:
        png = render_stats(s)
        await c.message.answer_photo(BufferedInputFile(png, filename="stats.png"), reply_markup=admin_kb())
    except Exception:
        logging.exception("stats image failed, sending text instead")
        await c.message.answer(stats_text(s), reply_markup=admin_kb())
    await c.answer()


async def next_report(target):
    x = await db.fetchrow("""select l.*, (select count(*) from reports where listing_id=l.id) rc
                             from listings l where status='hidden' order by id limit 1""")
    if not x:
        return await target.answer("Черга скарг порожня ✅", reply_markup=admin_kb())
    await target.answer(
        f"#{x['id']} | автор <code>{x['owner']}</code> | скарг: {x['rc']}\n<b>{esc(x['title'])}</b>\n"
        f"{esc(x['descr'])}\n🔗 <a href='{esc(x['link'])}'>{esc(x['link'])}</a>",
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
    await c.message.answer(f"Надішліть нове значення для «{SET_NAMES[k]}» (або «-», щоб очистити):",
                           reply_markup=cancel_kb("uk"))
    await c.answer()


@ar.message(Adm.val, F.text)
async def setting_save(m, lang, state: FSMContext):
    k = (await state.get_data())["key"]
    v = "" if m.text.strip() == "-" else m.text.strip()
    if k in NUM_KEYS and not v.isdigit():
        return await m.answer("Потрібне число.")
    if k == "sub_required" and v not in ("0", "1"):
        return await m.answer("Надішліть 0 або 1.")
    await set_setting(k, v)
    await state.clear()
    await m.answer("Збережено ✅", reply_markup=admin_kb())


@ar.callback_query(F.data == "adm:bc")
async def bc_ask(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.bc)
    await c.message.answer("Надішліть текст розсилки (HTML дозволено).", reply_markup=cancel_kb("uk"))
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


# --- moderation: disputes, user lookup ---
async def next_dispute(target):
    x = await db.fetchrow("select id from claims where status='disputed' order by disputed_at limit 1")
    if not x:
        return await target.answer("Спорів немає ✅", reply_markup=admin_kb())
    card = await claim_card(x["id"])
    await target.answer_photo(card["proof"], caption=dispute_caption(card), reply_markup=dispute_kb(card["id"]))


@ar.callback_query(F.data == "adm:disp")
async def disp_queue(c: CallbackQuery):
    await next_dispute(c.message)
    await c.answer()


@ar.callback_query(F.data.regexp(r"^dv:\d+:(ok|fake)$"))
async def dispute_decide(c: CallbackQuery):
    _, cid, act = c.data.split(":")
    ok = await (apply_confirmed(c.bot, int(cid)) if act == "ok" else reject_claim(c.bot, int(cid)))
    await c.answer("Готово" if ok else "Вже розглянуто")
    await next_dispute(c.message)


@ar.message(Command("user"))
async def user_info(m: Message, lang, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        return await m.answer("Використання: /user ID")
    uid = int(command.args)
    u = await db.fetchrow("select * from users where id=$1", uid)
    if not u:
        return await m.answer("Користувача не знайдено.")
    n = await db.fetchval("select count(*) from listings where owner=$1", uid)
    await m.answer(
        f"👤 <code>{uid}</code> @{esc(u['username'] or '—')}\n"
        f"Рейтинг: {u['score']} · страйки: {u['strikes']}\n"
        f"Зробив: {u['given']} · отримав: {u['received']} · борг: {max(0, u['received'] - u['given'])}\n"
        f"Штраф (відпрацювань): {u['penalty_due']} · анкет: {n}\n"
        f"Правила прийнято: {'так' if u['rules_ok'] else 'ні'} · бан: {'так' if u['banned'] else 'ні'}")


# --- support replies ---
async def next_ticket(target):
    x = await db.fetchrow("select id, uid, mid from support_tickets where status='open' order by id limit 1")
    if not x:
        return await target.answer("Звернень немає ✅", reply_markup=admin_kb())
    username = await db.fetchval("select username from users where id=$1", x["uid"])
    await target.bot.forward_message(target.chat.id, x["uid"], x["mid"])
    await target.answer(f"🆘 Звернення #{x['id']}\nВід: <code>{x['uid']}</code> @{esc(username or '—')}",
                        reply_markup=KB(inline_keyboard=[[B(text="✍️ Відповісти", callback_data=f"sr:{x['id']}")]]))


@ar.callback_query(F.data == "adm:sup")
async def sup_queue(c: CallbackQuery):
    await next_ticket(c.message)
    await c.answer()


@ar.callback_query(F.data.regexp(r"^sr:\d+$"))
async def sup_reply_ask(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.reply)
    await state.update_data(tid=int(c.data[3:]))
    await c.message.answer("Напишіть відповідь одним повідомленням (текст).", reply_markup=cancel_kb("uk"))
    await c.answer()


@ar.message(Adm.reply, F.text)
async def sup_reply_send(m: Message, state: FSMContext):
    tid = (await state.get_data())["tid"]
    await state.clear()
    x = await db.fetchrow("update support_tickets set status='closed' where id=$1 and status='open' returning uid", tid)
    if not x:
        return await m.answer("Звернення вже закрите.", reply_markup=admin_kb())
    try:
        await m.bot.send_message(x["uid"], t(await user_lang(x["uid"]), "sup_reply", text=esc(m.text)))
    except Exception:
        return await m.answer("Не вдалося надіслати: можливо, користувач заблокував бота.", reply_markup=admin_kb())
    await m.answer(f"Надіслано ✅ (звернення #{tid} закрито)", reply_markup=admin_kb())


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
        await claims_expiry(bot)
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
