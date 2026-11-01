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
    "claim_before_ask": ("Шаг 1 из 2. Пришлите скриншот ДО запуска: на нём должно быть видно, что вы ещё не запускали бота и не регистрировались. Без него заявка не создаётся.",
                         "Крок 1 з 2. Надішліть скриншот ДО запуску: на ньому має бути видно, що ви ще не запускали бота і не реєструвалися. Без нього заявка не створюється.",
                         "Step 1 of 2. Send a screenshot BEFORE you start: it must show you have not started the bot or signed up yet. Without it the claim is not created."),
    "claim_after_ask": ("Шаг 2 из 2. Пришлите скриншот ПОСЛЕ выполнения: регистрация или результат.",
                        "Крок 2 з 2. Надішліть скриншот ПІСЛЯ виконання: реєстрація або результат.",
                        "Step 2 of 2. Send a screenshot AFTER completing: the sign-up or the result."),
    "reason_ask": ("Укажите причину отказа одним сообщением: что именно не выполнено. Без причины отказ не отправится.",
                   "Вкажіть причину відмови одним повідомленням: що саме не виконано. Без причини відмова не надішлеться.",
                   "Give the reason for declining in one message: what was not done. Without a reason the decline is not sent."),
    "b_pm": ("🖼 Скринов: {n}", "🖼 Скрінів: {n}", "🖼 Screenshots: {n}"),
    "pm_set": ("Теперь нужно скриншотов: {n}", "Тепер потрібно скрінів: {n}", "Now screenshots required: {n}"),
    "sup_done": ("✅ Вопрос решён", "✅ Питання вирішено", "✅ Issue solved"),
    "sup_closed": ("Спасибо! Вопрос закрыт. Если понадобится, напишите в поддержку.",
                   "Дякуємо! Питання закрито. Якщо знову буде потрібно, напишіть у підтримку.",
                   "Thanks! The issue is closed. If you need more help, contact support again."),
    "sup_got": ("✅ Отправлено. Ответ придёт сюда, в этот чат.", "✅ Надіслано. Відповідь прийде сюди, у цей чат.",
                "✅ Sent. The reply will come here, in this chat."),
    "sup_admin_closed": ("✅ Администратор закрыл вопрос. Если нужно, напишите снова.",
                         "✅ Адміністратор закрив питання. Якщо потрібно, напишіть знову.",
                         "✅ An admin closed the issue. Write again if needed."),
    "b_tasks": ("📋 Мои задания", "📋 Мої завдання", "📋 My tasks"),
    "tasks_head": ("📋 <b>Мои задания</b>\n\nЗдесь видно, что вы уже сделали и что ждёт подтверждения автора.",
                   "📋 <b>Мої завдання</b>\n\nТут видно, що ви вже зробили і що чекає підтвердження автора.",
                   "📋 <b>My tasks</b>\n\nSee what you have already done and what is waiting for the author's confirmation."),
    "tk_pending": ("⏳ На просмотре у заказчика", "⏳ На перегляді у замовника", "⏳ Waiting for the author"),
    "tk_done": ("✅ Выполнено", "✅ Виконано", "✅ Done"),
    "tk_rejected": ("❌ Не засчитано", "❌ Не зараховано", "❌ Not counted"),
    "tk_empty": ("—", "—", "—"),
    "b_cancel": ("❌ Отменить", "❌ Відмінити", "❌ Cancel"),
    "open_link": ("🔗 Открыть ссылку", "🔗 Відкрити посилання", "🔗 Open link"),
    "b_skip": ("⏭ Пропустить", "⏭ Пропустити", "⏭ Skip"),
    "need_text": ("Нужен текст. Напишите сообщением или нажмите «Отменить».", "Потрібен текст. Напишіть повідомленням або натисніть «Відмінити».",
                  "Text is needed. Write a message or tap “Cancel”."),
    "b_rating": ("👤 Профиль", "👤 Профіль", "👤 Profile"),
    "b_rules": ("⚖️ Правила", "⚖️ Правила", "⚖️ Rules"),
    "b_back": ("◀️ Назад", "◀️ Назад", "◀️ Back"),
    "b_what": ("❓ Как это работает", "❓ Як це працює", "❓ How it works"),
    "b_strikes": ("⚠️ Фейки и баланс", "⚠️ Фейки і баланс", "⚠️ Fakes and balance"),
    "b_top": ("🥇 Топ", "🥇 Топ", "🥇 Top"),
    "b_accept": ("✅ Согласен", "✅ Погоджуюсь", "✅ I agree"),
    "claim": ("▶️ Выполнить", "▶️ Виконати", "▶️ Complete"),
    "yes_btn": ("✅ Подтвердить", "✅ Підтвердити", "✅ Confirm"),
    "no_btn": ("❌ Не было", "❌ Не було", "❌ Not done"),
    "rules_hidden": ("(скрыта: правила)", "(прихована: правила)", "(hidden: rules)"),
    "claim_ask": ("Пришлите скриншот регистрации по этой ссылке (фото). Без доказательства заявка не создаётся.",
                  "Надішліть скриншот реєстрації за цим посиланням (фото). Без доказу заявка не створюється.",
                  "Send a screenshot of your sign-up through this link (photo). Without proof the claim is not created."),
    "claim_need_photo": ("Нужен скриншот. Пришлите фото или /cancel.", "Потрібен скриншот. Надішліть фото або /cancel.",
                         "A screenshot is needed. Send a photo or /cancel."),
    "claim_sent": ("✅ Заявка отправлена автору. Если он не ответит за {d} ч., реферал засчитается автоматически.",
                   "✅ Заявку надіслано автору. Якщо він не відповість за {d} год., реферал зарахується автоматично.",
                   "✅ Claim sent to the author. If there is no answer in {d} hours, it counts automatically."),
    "claim_author": ("🔔 {who} утверждает, что выполнил задание на вашу анкету «{t}». Скриншот(ы) выше. Проверьте и подтвердите. Без ответа за {d} ч. засчитаем автоматически.",
                     "🔔 {who} стверджує, що виконав завдання на вашу анкету «{t}». Скриншот(и) вище. Перевірте і підтвердіть. Без відповіді за {d} год. зарахуємо автоматично.",
                     "🔔 {who} says they completed the task on your listing “{t}”. Screenshot(s) above. Check and confirm. No answer in {d} hours and it counts automatically."),
    "claim_ok": ("✅ Реферал засчитан. Рейтинг и баланс обновлены.", "✅ Реферал зараховано. Рейтинг і баланс оновлено.",
                 "✅ Referral counted. Rating and balance updated."),
    "claim_fake": ("⚠️ Заявка признана фейковой. Баланс −{n}.", "⚠️ Заявку визнано фейковою. Баланс −{n}.",
                   "⚠️ Claim marked as fake. Balance −{n}."),
    "claim_disputed": ("⚖️ Автор возразил: «{r}». Спор рассмотрит администратор, мы сообщим результат.",
                       "⚖️ Автор заперечив: «{r}». Спір розгляне адміністратор, ми повідомимо результат.",
                       "⚖️ The author disputed it: “{r}”. An admin will review it and we will let you know."),
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
    "claim_remind": ("⏰ Через 2 часа заявка на «{t}» засчитается автоматически. Проверьте её и подтвердите или отклоните.",
                     "⏰ Через 2 години заявка на «{t}» зарахується автоматично. Перевірте її і підтвердіть або відхиліть.",
                     "⏰ In 2 hours the claim on “{t}” counts automatically. Check it and confirm or decline."),
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
    "claim_pair": ("По этому автору заявка уже открыта. Дождитесь ответа.",
                   "По цьому автору заявка вже відкрита. Дочекайтесь відповіді.",
                   "A claim for this author is already open. Wait for the answer."),
    "rules_text": ("⚖️ <b>Правила взаимных рефералов</b>\n\n1. Реферал — реальная регистрация по ссылке автора. Фейки запрещены.\n2. Баланс: +1, когда вы сделали реферал, и −1, когда сделали вам. Баланс −{debt} и ниже — анкета скрыта, пока не выровняете.\n3. У автора {hours} ч. на ответ. Без ответа заявка засчитывается сама.\n4. Фейк, подтверждённый администратором: баланс −{pen}.\n\nНажимая «Согласен», вы принимаете эти правила.",
                   "⚖️ <b>Правила взаємних рефералів</b>\n\n1. Реферал — реальна реєстрація за посиланням автора. Фейки заборонені.\n2. Баланс: +1, коли ви зробили реферал, і −1, коли зробили вам. Баланс −{debt} і нижче — анкета прихована, поки не вирівняєте.\n3. Автор має {hours} год. на відповідь. Без відповіді заявка зараховується сама.\n4. Фейк, підтверджений адміністратором: баланс −{pen}.\n\nНатискаючи «Погоджуюсь», ви приймаєте ці правила.",
                   "⚖️ <b>Mutual referral rules</b>\n\n1. A referral is a real sign-up through the author's link. Fakes are not allowed.\n2. Balance: +1 when you make a referral, −1 when someone makes one for you. A balance of −{debt} or lower hides your listing until you balance it.\n3. The author has {hours} hours to answer. No answer and the claim counts automatically.\n4. A fake confirmed by an admin: balance −{pen}.\n\nBy tapping “I agree” you accept these rules."),
    "rt_main": ("👤 <b>Ваш профиль</b>\n\n⚖️ Баланс: <b>{b}</b>\n🏆 Рейтинг (сделано рефералов): {r}\n👍 Подтверждаете работы: {p}\n⚠️ Фейков: {f}",
                "👤 <b>Ваш профіль</b>\n\n⚖️ Баланс: <b>{b}</b>\n🏆 Рейтинг (зроблено рефералів): {r}\n👍 Погоджуєте роботи: {p}\n⚠️ Фейків: {f}",
                "👤 <b>Your profile</b>\n\n⚖️ Balance: <b>{b}</b>\n🏆 Rating (referrals made): {r}\n👍 You approve: {p}\n⚠️ Fakes: {f}"),
    "rt_what_text": ("❓ <b>Как это работает</b>\n\n⚖️ Баланс: +1 за каждый реферал, который вы сделали. −1 за каждый, который сделали вам. Анкета прячется при балансе −{debt}.\n🏆 Рейтинг — сколько рефералов вы сделали. Растёт только от честных рефералов.\n👍 Процент — сколько заявок вы подтвердили, а не отклонили.",
                     "❓ <b>Як це працює</b>\n\n⚖️ Баланс: +1 за кожен реферал, який ви зробили. −1 за кожен, який зробили вам. Анкета ховається при балансі −{debt}.\n🏆 Рейтинг — скільки рефералів ви зробили. Росте лише від чесних рефералів.\n👍 Відсоток — скільки заявок ви погодили, а не відхилили.",
                     "❓ <b>How it works</b>\n\n⚖️ Balance: +1 for each referral you make, −1 for each one made for you. Your listing hides at −{debt}.\n🏆 Rating: how many referrals you made. It grows only from honest referrals.\n👍 Percentage: how many claims you approved versus declined."),
    "rt_strikes_text": ("⚠️ <b>Фейки и баланс</b>\n\nФейк — заявка, которую администратор признал ложной. За каждый фейк баланс −{pen}.\nВернуть баланс можно, сделав рефералы для других пользователей. Пока баланс −{debt} или ниже, анкета скрыта из каталога.",
                        "⚠️ <b>Фейки і баланс</b>\n\nФейк — заявка, яку адміністратор визнав неправдивою. За кожен фейк баланс −{pen}.\nПовернути баланс можна, зробивши реферали для інших. Поки баланс −{debt} або нижче, анкета прихована з каталогу.",
                        "⚠️ <b>Fakes and balance</b>\n\nA fake is a claim an admin ruled false. Each fake: balance −{pen}.\nYou restore the balance by making referrals for others. While the balance is −{debt} or lower, your listing is hidden from the catalog."),
    "rt_top_title": ("🥇 <b>Топ по рейтингу</b>\n\n", "🥇 <b>Топ за рейтингом</b>\n\n", "🥇 <b>Rating top</b>\n\n"),
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
    "pct_fmt": ("{p}% из {n} решений", "{p}% з {n} рішень", "{p}% of {n} decisions"),
    "pct_none": ("ещё нет решений", "ще немає рішень", "no decisions yet"),
    "card_info": ("⚖️ Баланс автора: {b} · 🏆 Рейтинг: {r}\n👍 Подтверждает: {p}\n📸 Скриншотов: {s}",
                  "⚖️ Баланс автора: {b} · 🏆 Рейтинг: {r}\n👍 Погоджує: {p}\n📸 Скрінів: {s}",
                  "⚖️ Author balance: {b} · 🏆 Rating: {r}\n👍 Approves: {p}\n📸 Screenshots: {s}"),
    "shots_1": ("1 (после выполнения)", "1 (після виконання)", "1 (after completing)"),
    "shots_2": ("2 (до запуска и после)", "2 (до запуску і після)", "2 (before and after)"),
    "lock_balance": ("🔒 Анкеты скрыты из каталога: баланс {b}. Сделайте рефералы для других, и они вернутся автоматически.",
                     "🔒 Анкети приховано з каталогу: баланс {b}. Зробіть реферали для інших, і вони повернуться автоматично.",
                     "🔒 Your listings are hidden: balance {b}. Make referrals for others and they come back automatically."),
}


def t(lang, key, **kw):
    s = T[key][LANGS.index(lang)]
    return s.format(**kw) if kw else s


DEFAULTS = {"ttl_days": "30", "report_threshold": "3", "sub_required": "0", "sub_channel": "",
            "welcome_ru": "", "welcome_uk": "", "welcome_en": "",
            "claim_hours": "12", "open_claims_max": "3", "pair_cooldown_days": "30",
            "debt_limit": "2", "penalty_per_strike": "3", "disputes_per_10": "5"}
NUM_KEYS = ("ttl_days", "report_threshold", "claim_hours", "open_claims_max",
            "pair_cooldown_days", "debt_limit", "penalty_per_strike", "disputes_per_10")


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
alter table listings add column if not exists proof_mode int not null default 1;
alter table claims add column if not exists proof_before text;
alter table claims add column if not exists mid bigint;
alter table claims add column if not exists mid_before bigint;
alter table claims add column if not exists reason text;
create table if not exists chats(uid bigint primary key, status text not null default 'open',
  updated timestamptz not null default now());
create table if not exists chat_msgs(id bigserial primary key, uid bigint not null, from_admin boolean not null,
  text text not null, created timestamptz not null default now());
create index if not exists chat_msgs_uid on chat_msgs(uid, id);
do $$ begin
  if not exists (select 1 from information_schema.columns where table_name='users' and column_name='balance') then
    alter table users add column balance int not null default 0;
    alter table users add column rating int not null default 0;
    alter table users add column fakes int not null default 0;
    update users set balance = given - received - 3*strikes, rating = given, fakes = strikes;
    update listings set rules_hidden = exists(select 1 from users u where u.id = listings.owner and u.balance <= -2);
  end if;
end $$;
alter table claims add column if not exists auto boolean not null default false;
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
    """Hides the owner's listings while the balance is at or below -debt_limit. Tells the owner once per change."""
    u = await db.fetchrow("select balance, locked from users where id=$1", uid)
    if not u:
        return
    locked = u["balance"] <= -int(await setting("debt_limit"))
    await db.execute("update listings set rules_hidden=$2 where owner=$1", uid, locked)
    if locked == u["locked"]:
        return
    await db.execute("update users set locked=$2 where id=$1", uid, locked)
    if bot is None or not await db.fetchval("select exists(select 1 from listings where owner=$1)", uid):
        return
    if locked:
        await notify(bot, uid, "lock_balance", b=f"{u['balance']:+d}")
    else:
        await notify(bot, uid, "unlock")


async def claim_block_reason(uid, owner, lang):
    """Only one open claim per pair of users."""
    busy = await db.fetchval(
        "select 1 from claims where giver=$1 and receiver=$2 and status in ('pending','disputed') limit 1", uid, owner)
    return t(lang, "claim_pair") if busy else None


async def owner_stats(uid):
    """How often the owner approves (confirms by hand) versus declines (opens a dispute)."""
    r = await db.fetchrow("""select
        count(*) filter (where status='confirmed' and disputed_at is null and not auto) ok,
        count(*) filter (where disputed_at is not null) nope
        from claims where receiver=$1""", uid)
    total = r["ok"] + r["nope"]
    return {"ok": r["ok"], "nope": r["nope"], "total": total,
            "pct": round(100 * r["ok"] / total) if total else None}


async def claim_card(cid):
    return await db.fetchrow(
        """select c.id, c.giver, c.receiver, c.proof, c.reason, l.id lid, l.title
           from claims c join listings l on l.id=c.listing_id where c.id=$1""", cid)


async def apply_confirmed(bot, cid, auto=False):
    """Counts a referral: giver +1 balance and +1 rating, receiver -1 balance. Guarded by status against double counting."""
    row = await db.fetchrow("""update claims set status='confirmed', decided=now(), auto=$2
        where id=$1 and status in ('pending','disputed') returning giver, receiver""", cid, auto)
    if not row:
        return False
    g, rc = row["giver"], row["receiver"]
    await db.execute("update users set balance=balance+1, rating=rating+1 where id=$1", g)
    await db.execute("update users set balance=balance-1 where id=$1", rc)
    await refresh_rules(g, bot)
    await refresh_rules(rc, bot)
    await notify(bot, g, "claim_ok")
    await notify(bot, rc, "claim_ok")
    return True


async def reject_claim(bot, cid):
    """An admin rules the claim fake: the executor loses penalty_per_strike from balance."""
    row = await db.fetchrow("""update claims set status='rejected', decided=now()
        where id=$1 and status in ('pending','disputed') returning giver""", cid)
    if not row:
        return False
    per = int(await setting("penalty_per_strike"))
    await db.execute("update users set fakes=fakes+1, balance=balance-$2 where id=$1", row["giver"], per)
    await refresh_rules(row["giver"], bot)
    await notify(bot, row["giver"], "claim_fake", n=per)
    return True


async def send_claim_to_author(bot, cid):
    card = await db.fetchrow("""select c.id, c.giver, c.receiver, c.proof, c.mid, c.proof_before, c.mid_before, l.title
        from claims c join listings l on l.id=c.listing_id where c.id=$1""", cid)
    lang = await user_lang(card["receiver"])
    gname = await db.fetchval("select username from users where id=$1", card["giver"])
    who = f"@{esc(gname)} (<code>{card['giver']}</code>)" if gname else f"<code>{card['giver']}</code>"
    try:
        if card["proof_before"]:
            await forward_proof(bot, card["receiver"], card["giver"], card["mid_before"], card["proof_before"])
        await forward_proof(bot, card["receiver"], card["giver"], card["mid"], card["proof"])
        kb = KB(inline_keyboard=[[B(text=t(lang, "yes_btn"), callback_data=f"cf:{cid}"),
                                  B(text=t(lang, "no_btn"), callback_data=f"cd:{cid}")]])
        await bot.send_message(card["receiver"], t(lang, "claim_author", who=who, t=esc(card["title"]),
                                                   d=await setting("claim_hours")), reply_markup=kb)
    except Exception:
        logging.exception("could not send the claim to the author")


def dispute_caption(card):
    return (f"⚖️ Спір #{card['id']}\nЗаявник: <code>{card['giver']}</code>\nАвтор: <code>{card['receiver']}</code>\n"
            f"Анкета #{card['lid']} «{esc(card['title'])}»\nПричина автора: {esc(card['reason'] or '—')}\n"
            f"Докази заявника — вище.")


def dispute_kb(card):
    return KB(inline_keyboard=[
        [B(text="✅ Зарахувати", callback_data=f"dv:{card['id']}:ok"),
         B(text="❌ Фейк", callback_data=f"dv:{card['id']}:fake")],
        [B(text="✉️ Заявнику", callback_data=f"chw:{card['giver']}"),
         B(text="✉️ Автору", callback_data=f"chw:{card['receiver']}")]])


async def notify_admins_dispute(bot, cid):
    card = await claim_card(cid)
    for a in ADMIN_IDS:
        try:
            await bot.send_photo(a, card["proof"], caption=dispute_caption(card), reply_markup=dispute_kb(card))
        except Exception:
            logging.exception("could not send the dispute to admin")


async def claims_expiry(bot):
    """Reminds the author two hours before the deadline, auto-counts unanswered claims,
    closes disputes nobody decided within 7 days. Runs hourly, so auto-counting can be up to an hour late."""
    try:
        hours = int(await setting("claim_hours"))
        if hours > 2:
            for x in await db.fetch("""select c.id, c.receiver, l.title from claims c join listings l on l.id=c.listing_id
                    where c.status='pending' and not c.reminded and c.created < now() - make_interval(hours => $1)""", hours - 2):
                await db.execute("update claims set reminded=true where id=$1", x["id"])
                await notify(bot, x["receiver"], "claim_remind", t=esc(x["title"]))
        for x in await db.fetch("select id from claims where status='pending' and created < now() - make_interval(hours => $1)", hours):
            await apply_confirmed(bot, x["id"], auto=True)
        await db.execute("""update claims set status='expired', decided=now()
            where status='disputed' and disputed_at < now() - interval '7 days'""")
    except Exception:
        logging.exception("claims expiry failed")


async def rules_caption(lang):
    return t(lang, "rules_text", hours=await setting("claim_hours"), pen=await setting("penalty_per_strike"),
             debt=await setting("debt_limit"))


async def rating_text(uid, lang):
    u = await db.fetchrow("select balance, rating, fakes from users where id=$1", uid)
    s = await owner_stats(uid)
    pct = t(lang, "pct_fmt", p=s["pct"], n=s["total"]) if s["total"] else t(lang, "pct_none")
    return t(lang, "rt_main", b=f"{u['balance']:+d}", r=u["rating"], p=pct, f=u["fakes"])


def rating_kb(lang):
    return KB(inline_keyboard=[
        [B(text=t(lang, "b_what"), callback_data="rt:what"), B(text=t(lang, "b_strikes"), callback_data="rt:strikes")],
        [B(text=t(lang, "b_top"), callback_data="rt:top"), B(text=t(lang, "b_rules"), callback_data="rules:view")],
        [B(text=t(lang, "b_tasks"), callback_data="tasks")],
        [B(text="🏠", callback_data="menu")]])


def rules_kb(lang, accept=True):
    rows = [[B(text=t(lang, "b_accept"), callback_data="rules:ok")]] if accept else []
    rows.append([B(text="🏠", callback_data="menu")])
    return KB(inline_keyboard=rows)


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
    rows = [[b("b_cat", "cat")], [b("b_new", "new"), b("b_my", "my")], [b("b_rating", "rt"), b("b_support", "sup")], [b("b_lang", "lang")]]
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
    chat = State()


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
        f"select l.*, u.username, u.balance, u.rating from listings l join users u on u.id=l.owner where {cond} "
        f"order by l.updated desc, l.id offset {p} limit 1", *args)
    await log_event(c.from_user.id, "view")
    s = await owner_stats(x["owner"])
    pct = t(lang, "pct_fmt", p=s["pct"], n=s["total"]) if s["total"] else t(lang, "pct_none")
    shots = t(lang, "shots_2") if x["proof_mode"] == 2 else t(lang, "shots_1")
    info = t(lang, "card_info", b=f"{x['balance']:+d}", r=x["rating"], p=pct, s=shots)
    text = f"{t(lang, 'c_' + x['category'])}\n<b>{esc(x['title'])}</b>"
    if x["descr"]:
        text += f"\n{esc(x['descr'])}"
    text += f"\n\n{info}"
    rows = []
    if x["owner"] == c.from_user.id:
        text += f"\n\n<i>{t(lang, 'yours')}</i>"
    else:
        if x["username"]:
            url = f"https://t.me/{x['username']}"
        else:
            url = f"tg://user?id={x['owner']}"
            text += f"\n\n<i>{t(lang, 'hint')}</i>"
        rows += [[B(text=t(lang, "open_link"), url=x["link"])],
                 [B(text=t(lang, "claim"), callback_data=f"cl:{x['id']}")],
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
                                              B(text=t(lang, "delete"), callback_data=f"ld:{x['id']}")],
                                              [B(text=t(lang, "open_link"), url=x["link"])],
                                              [B(text=t(lang, "b_pm", n=x["proof_mode"]), callback_data=f"pm:{x['id']}")]]))
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


# --- my tasks: what I did and what waits for the author ---
async def tasks_text(uid, lang):
    rows = await db.fetch("""select c.status, c.created, l.title from claims c
        join listings l on l.id=c.listing_id where c.giver=$1 order by c.created desc limit 200""", uid)

    def block(statuses, label):
        items = [x for x in rows if x["status"] in statuses]
        body = "\n".join(f"• {esc(x['title'][:30])} · {x['created']:%d.%m}" for x in items[:4])
        if not items:
            body = t(lang, "tk_empty")
        more = f"\n… ще {len(items) - 4}" if len(items) > 4 else ""
        return f"<b>{t(lang, label)} ({len(items)})</b>\n{body}{more}"

    return "\n\n".join([block(("pending", "disputed"), "tk_pending"),
                         block(("confirmed",), "tk_done"),
                         block(("rejected", "expired"), "tk_rejected")])


@r.callback_query(F.data == "tasks")
async def tasks_view(c: CallbackQuery, lang):
    await screen(c, "saved", t(lang, "tasks_head") + "\n\n" + await tasks_text(c.from_user.id, lang),
                 sub_kb(lang, back="menu"))
    await c.answer()


# --- rules, rating and referral claims ---
class Claim(StatesGroup):
    before = State()
    after = State()
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
    await screen(c, "rating", t(lang, "rt_what_text", debt=await setting("debt_limit")), sub_kb(lang))
    await c.answer()


@r.callback_query(F.data == "rt:strikes")
async def rating_strikes(c: CallbackQuery, lang):
    await screen(c, "rating", t(lang, "rt_strikes_text", pen=await setting("penalty_per_strike"),
                                debt=await setting("debt_limit")), sub_kb(lang))
    await c.answer()


@r.callback_query(F.data == "rt:top")
async def rating_top(c: CallbackQuery, lang):
    rows = await db.fetch("select username, rating from users where rating>0 and not banned order by rating desc, id limit 10")
    body = "\n".join(f"{i}. {('@' + esc(x['username'])) if x['username'] else '—'} · {x['rating']}"
                     for i, x in enumerate(rows, 1))
    await screen(c, "rating", t(lang, "rt_top_title") + (body if rows else t(lang, "rt_top_empty")), sub_kb(lang))
    await c.answer()


class Reason(StatesGroup):
    text = State()


class Support(StatesGroup):
    write = State()


async def forward_proof(bot, to_uid, from_uid, mid, fid):
    """Forwards the executor's screenshot, so the author sees who sent it. Falls back to a plain copy."""
    try:
        if mid is None:
            raise ValueError("no message id")
        await bot.forward_message(to_uid, from_uid, mid)
    except Exception:
        await bot.send_photo(to_uid, fid)


async def claim_create(m: Message, lang, lid, proof, mid, before=None, mid_before=None):
    x = await db.fetchrow("select owner from listings where id=$1 and status='active' and not rules_hidden", lid)
    if not x:
        return await m.answer(t(lang, "claim_gone"))
    reason = await claim_block_reason(m.from_user.id, x["owner"], lang)
    if reason:
        return await m.answer(reason)
    cid = await db.fetchval(
        """insert into claims(listing_id, giver, receiver, proof, mid, proof_before, mid_before)
           values($1,$2,$3,$4,$5,$6,$7) returning id""",
        lid, m.from_user.id, x["owner"], proof, mid, before, mid_before)
    await m.answer(t(lang, "claim_sent", d=await setting("claim_hours")))
    await send_claim_to_author(m.bot, cid)


@r.callback_query(F.data.startswith("cl:"))
async def claim_start(c: CallbackQuery, lang, state: FSMContext):
    lid = int(c.data[3:])
    x = await db.fetchrow("select id, owner, proof_mode from listings where id=$1 and status='active' and not rules_hidden", lid)
    if not x:
        return await c.answer(t(lang, "claim_gone"), show_alert=True)
    if x["owner"] == c.from_user.id:
        return await c.answer()
    reason = await claim_block_reason(c.from_user.id, x["owner"], lang)
    if reason:
        return await c.answer(reason, show_alert=True)
    if x["proof_mode"] == 2:
        await state.set_state(Claim.before)
        ask = "claim_before_ask"
    else:
        await state.set_state(Claim.proof)
        ask = "claim_ask"
    await state.update_data(lid=lid)
    await send_screen(c.bot, c.message.chat.id, "proof", t(lang, ask), cancel_kb(lang))
    await c.answer()


@r.message(Claim.before, F.photo)
async def claim_before(m: Message, lang, state: FSMContext):
    await state.update_data(before=m.photo[-1].file_id, mid_before=m.message_id)
    await state.set_state(Claim.after)
    await m.answer(t(lang, "claim_after_ask"), reply_markup=cancel_kb(lang))


@r.message(Claim.after, F.photo)
async def claim_after(m: Message, lang, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    await claim_create(m, lang, d["lid"], m.photo[-1].file_id, m.message_id, d.get("before"), d.get("mid_before"))


@r.message(Claim.proof, F.photo)
async def claim_proof(m: Message, lang, state: FSMContext):
    lid = (await state.get_data())["lid"]
    await state.clear()
    await claim_create(m, lang, lid, m.photo[-1].file_id, m.message_id)


@r.message(StateFilter(Claim.before, Claim.after, Claim.proof))
async def claim_need_photo_msg(m: Message, lang):
    await m.answer(t(lang, "claim_need_photo"), reply_markup=cancel_kb(lang))


@r.callback_query(F.data.regexp(r"^c[fd]:\d+$"))
async def claim_answer(c: CallbackQuery, lang, state: FSMContext):
    cid = int(c.data[3:])
    row = await db.fetchrow("select status, receiver, giver from claims where id=$1", cid)
    if not row or row["receiver"] != c.from_user.id or row["status"] != "pending":
        return await c.answer(t(lang, "claim_done"), show_alert=True)
    if c.data.startswith("cf:"):
        await apply_confirmed(c.bot, cid)
        await c.answer(t(lang, "done"))
    else:
        await state.set_state(Reason.text)
        await state.update_data(cid=cid)
        await c.message.answer(t(lang, "reason_ask"), reply_markup=cancel_kb(lang))
        await c.answer()
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@r.message(Reason.text, F.text)
async def reason_text(m: Message, lang, state: FSMContext):
    reason = m.text.strip()[:300]
    if len(reason) < 3:
        return await m.answer(t(lang, "reason_ask"), reply_markup=cancel_kb(lang))
    cid = (await state.get_data())["cid"]
    await state.clear()
    row = await db.fetchrow("""update claims set status='disputed', disputed_at=now(), reason=$2
        where id=$1 and status='pending' returning giver""", cid, reason)
    if not row:
        return await m.answer(t(lang, "claim_done"), reply_markup=home_kb())
    await send_screen(m.bot, row["giver"], "dispute",
                      t(await user_lang(row["giver"]), "claim_disputed", r=esc(reason)))
    await notify_admins_dispute(m.bot, cid)
    await m.answer(t(lang, "dispute_sent"), reply_markup=home_kb())


@r.message(Reason.text)
async def reason_need_text(m: Message, lang):
    await m.answer(t(lang, "reason_ask"), reply_markup=cancel_kb(lang))


@r.callback_query(F.data.startswith("pm:"))
async def proof_mode(c: CallbackQuery, lang):
    lid = int(c.data[3:])
    n = await db.fetchval(
        "update listings set proof_mode = case when proof_mode=1 then 2 else 1 end where id=$1 and owner=$2 returning proof_mode",
        lid, c.from_user.id)
    if not n:
        return await c.answer()
    await c.answer(t(lang, "pm_set", n=n), show_alert=True)


# --- support: the user writes, admins answer in a chat ---
def support_kb(lang):
    return KB(inline_keyboard=[[B(text=t(lang, "sup_done"), callback_data="supdone")],
                               [B(text="🏠", callback_data="menu")]])


@r.callback_query(F.data == "sup")
async def support_start(c: CallbackQuery, lang, state: FSMContext):
    await state.set_state(Support.write)
    await send_screen(c.bot, c.message.chat.id, "support", t(lang, "sup_ask"), support_kb(lang))
    await c.answer()


@r.message(Support.write)
async def support_msg(m: Message, lang, state: FSMContext):
    await chat_log(m.from_user.id, False, m.text or m.caption or "[медіа]")
    for a in ADMIN_IDS:
        try:
            await m.forward(a)
            await m.bot.send_message(
                a, f"💬 Нове повідомлення від <code>{m.from_user.id}</code> @{esc(m.from_user.username or '—')}",
                reply_markup=KB(inline_keyboard=[[B(text="💬 Відкрити чат", callback_data=f"ch:{m.from_user.id}")]]))
        except Exception:
            logging.exception("could not forward support message to admin")
    await m.answer(t(lang, "sup_got"), reply_markup=support_kb(lang))


@r.callback_query(F.data == "supdone")
async def support_done(c: CallbackQuery, lang, state: FSMContext):
    await state.clear()
    await db.execute("update chats set status='closed', updated=now() where uid=$1", c.from_user.id)
    for a in ADMIN_IDS:
        try:
            await c.bot.send_message(a, f"✅ Користувач <code>{c.from_user.id}</code> закрив питання.")
        except Exception:
            pass
    await send_screen(c.bot, c.message.chat.id, "support", t(lang, "sup_closed"), home_kb())
    await c.answer()


# ---------- ADMIN ROUTER (ukrainian only) ----------
ar = Router()
ar.message.filter(F.from_user.id.in_(ADMIN_IDS))
ar.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

SET_NAMES = {"ttl_days": "Термін життя анкети (дні)", "report_threshold": "Поріг скарг для приховування",
             "sub_required": "Обов'язкова підписка (1 - так, 0 - ні)", "sub_channel": "Канал (@username або ID)",
             "claim_hours": "Годин на відповідь автора (автозарахування)",
             "debt_limit": "Баланс, від якого анкета ховається (2 = від −2)",
             "penalty_per_strike": "Штраф балансу за фейк",
             "welcome_ru": "Привітання RU", "welcome_uk": "Привітання UK", "welcome_en": "Привітання EN"}


def admin_kb():
    rows = [("📊 Статистика", "adm:stats"), ("⚖️ Спори", "adm:disp"), ("💬 Чати", "adm:chats"), ("⚠️ Скарги", "adm:rep"),
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
                                         [B(text="🚫 Забанити автора", callback_data=f"ab:{x['id']}")],
                                         [B(text="✉️ Написати автору", callback_data=f"chw:{x['owner']}")]]))


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
    await target.answer_photo(card["proof"], caption=dispute_caption(card), reply_markup=dispute_kb(card))


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


# --- chats with users, user cards and balance controls ---
async def chat_log(uid, from_admin, text):
    await db.execute("insert into chat_msgs(uid, from_admin, text) values($1,$2,$3)", uid, from_admin, text[:4000])
    await db.execute("""insert into chats(uid, status, updated) values($1,'open',now())
        on conflict(uid) do update set status='open', updated=now()""", uid)


def chat_kb(uid):
    return KB(inline_keyboard=[
        [B(text="✍️ Написати", callback_data=f"chw:{uid}")],
        [B(text="✅ Закрити питання", callback_data=f"chx:{uid}"), B(text="🚪 Вийти", callback_data="adm")],
        [B(text="👤 Картка користувача", callback_data=f"uc:{uid}")]])


async def chat_view(target, uid):
    u = await db.fetchrow("select username from users where id=$1", uid)
    st = await db.fetchval("select status from chats where uid=$1", uid) or "—"
    msgs = await db.fetch("select from_admin, text, created from chat_msgs where uid=$1 order by id desc limit 10", uid)
    body = "\n\n".join(
        f"<b>{'🛠 Адмін' if x['from_admin'] else '👤 Користувач'}</b> · {x['created']:%d.%m %H:%M}\n{esc(x['text'][:250])}"
        for x in reversed(msgs)) or "Історія порожня."
    name = esc(u["username"]) if u and u["username"] else "—"
    await target.answer(f"💬 <b>Чат з</b> <code>{uid}</code> @{name} · {st}\n\n{body}", reply_markup=chat_kb(uid))


@ar.callback_query(F.data == "adm:chats")
async def chats_list(c: CallbackQuery):
    rows = await db.fetch("""select ch.uid, u.username from chats ch left join users u on u.id=ch.uid
        where ch.status='open' order by ch.updated desc limit 15""")
    if not rows:
        await c.message.answer("Відкритих чатів немає ✅", reply_markup=admin_kb())
    else:
        kb = [[B(text=f"{x['uid']} @{x['username'] or '—'}", callback_data=f"ch:{x['uid']}")] for x in rows]
        kb.append([B(text="🚪 Адмін-панель", callback_data="adm")])
        await c.message.answer("💬 Відкриті чати (оберіть):", reply_markup=KB(inline_keyboard=kb))
    await c.answer()


@ar.callback_query(F.data.regexp(r"^ch:\d+$"))
async def chat_open(c: CallbackQuery):
    await chat_view(c.message, int(c.data[3:]))
    await c.answer()


@ar.callback_query(F.data.regexp(r"^chw:\d+$"))
async def chat_write(c: CallbackQuery, state: FSMContext):
    uid = int(c.data[4:])
    await state.set_state(Adm.chat)
    await state.update_data(uid=uid)
    await c.message.answer(f"✍️ Пишіть повідомлення для <code>{uid}</code>. Кожне піде користувачу. «🚪 Вийти» — завершити.",
                           reply_markup=chat_kb(uid))
    await c.answer()


@ar.message(Adm.chat, F.text)
async def chat_send(m: Message, state: FSMContext):
    uid = (await state.get_data())["uid"]
    await chat_log(uid, True, m.text)
    try:
        await m.bot.send_message(uid, t(await user_lang(uid), "sup_reply", text=esc(m.text)))
        note = "Надіслано ✅"
    except Exception:
        note = "Не вдалося доставити: можливо, користувач заблокував бота. Текст збережено в історії."
    await m.answer(note, reply_markup=chat_kb(uid))


@ar.callback_query(F.data.regexp(r"^chx:\d+$"))
async def chat_close(c: CallbackQuery):
    uid = int(c.data[4:])
    await db.execute("update chats set status='closed', updated=now() where uid=$1", uid)
    try:
        await c.bot.send_message(uid, t(await user_lang(uid), "sup_admin_closed"))
    except Exception:
        pass
    await c.answer("Питання закрито")
    await chat_view(c.message, uid)


async def user_card(uid):
    u = await db.fetchrow("select * from users where id=$1", uid)
    if not u:
        return None, None
    n = await db.fetchval("select count(*) from listings where owner=$1", uid)
    s = await owner_stats(uid)
    pct = f"{s['pct']}% ({s['total']})" if s["total"] else "—"
    text = (f"👤 <code>{uid}</code> @{esc(u['username'] or '—')}\n"
            f"Баланс: <b>{u['balance']:+d}</b> · Рейтинг: {u['rating']} · Фейків: {u['fakes']}\n"
            f"Погоджує: {pct} · Анкет: {n}\n"
            f"Правила: {'так' if u['rules_ok'] else 'ні'} · Бан: {'так' if u['banned'] else 'ні'}")
    kb = KB(inline_keyboard=[
        [B(text="⚖️ Баланс +1", callback_data=f"uadj:{uid}:balance:1"),
         B(text="⚖️ Баланс −1", callback_data=f"uadj:{uid}:balance:-1")],
        [B(text="✉️ Написати", callback_data=f"chw:{uid}"), B(text="💬 Чат", callback_data=f"ch:{uid}")],
        [B(text="🔄 Оновити", callback_data=f"uc:{uid}")]])
    return text, kb


async def show_card(c: CallbackQuery, uid):
    text, kb = await user_card(uid)
    if text is None:
        await c.answer("Користувача не знайдено", show_alert=True)
        return
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except Exception:
        await c.message.answer(text, reply_markup=kb)
    await c.answer()


@ar.message(Command("user"))
async def user_info(m: Message, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        return await m.answer("Використання: /user ID")
    text, kb = await user_card(int(command.args))
    await m.answer(text or "Користувача не знайдено.", reply_markup=kb)


@ar.callback_query(F.data.regexp(r"^uc:\d+$"))
async def user_card_cb(c: CallbackQuery):
    await show_card(c, int(c.data[3:]))


@ar.callback_query(F.data.regexp(r"^uadj:\d+:balance:-?\d+$"))
async def user_adjust(c: CallbackQuery):
    _, uid, _field, d = c.data.split(":")
    await db.execute("update users set balance = balance + $2 where id=$1", int(uid), int(d))
    await refresh_rules(int(uid), c.bot)
    await show_card(c, int(uid))


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
