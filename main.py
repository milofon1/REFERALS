import asyncio, hashlib, html, json, logging, os, re, time
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
from aiogram.fsm.storage.base import BaseStorage
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton as B,
                           InlineKeyboardMarkup as KB, InputMediaPhoto, Message)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

# ---------- CONFIG ----------
TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]
BASE_URL = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
INSTANCE = os.urandom(6).hex()  # tells two Render instances apart during a deploy
SUP_LIMIT, SUP_WINDOW_MIN = 5, 10  # support: at most 5 user messages per 10 minutes


def clean_dsn(url):
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "channel_binding"]
    return urlunparse(p._replace(query=urlencode(q)))


DSN = clean_dsn(os.environ["DATABASE_URL"])
db: asyncpg.Pool = None
esc = html.escape
COIN = "🪙"

# ---------- TEXTS (ru, uk, en) ----------
LANGS = ("ru", "uk", "en")
T = {
    "choose": ("Выберите язык / Оберіть мову / Choose language",) * 3,
    "welcome": ("Привет! Здесь можно найти людей для взаимных рефералов.\n\nВыполняйте чужие задания, получайте монеты 🪙 и платите ими тем, кто выполнит ваши.",
                "Привіт! Тут можна знайти людей для взаємних рефералів.\n\nВиконуйте чужі завдання, отримуйте монети 🪙 і платіть ними тим, хто виконає ваші.",
                "Hi! Find people for mutual referrals here.\n\nComplete other people's tasks, earn coins 🪙 and pay them to those who complete yours."),
    "menu": ("Меню · 🪙 свободно: <b>{f}</b>", "Меню · 🪙 вільно: <b>{f}</b>", "Menu · 🪙 available: <b>{f}</b>"),
    "b_new": ("➕ Новая анкета", "➕ Нова анкета", "➕ New listing"),
    "b_cat": ("🔎 Каталог", "🔎 Каталог", "🔎 Catalog"),
    "b_my": ("📋 Мои анкеты", "📋 Мої анкети", "📋 My listings"),
    "b_lang": ("🌐 Язык", "🌐 Мова", "🌐 Language"),
    "b_coins": ("🪙 Монеты", "🪙 Монети", "🪙 Coins"),
    "b_notif": ("🔔 Уведомления", "🔔 Сповіщення", "🔔 Notifications"),
    "b_invite": ("🤝 Пригласить друга", "🤝 Запросити друга", "🤝 Invite a friend"),
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
    "saved": ("✅ Анкета опубликована!\n\nЗа каждое выполненное задание вы платите {p} 🪙. Цену можно изменить в «Мои анкеты».",
              "✅ Анкету опубліковано!\n\nЗа кожне виконане завдання ви платите {p} 🪙. Ціну можна змінити в «Мої анкети».",
              "✅ Listing published!\n\nYou pay {p} 🪙 for each completed task. Change the price in “My listings”."),
    "empty": ("Пока ничего нет.", "Поки нічого немає.", "Nothing here yet."),
    "write": ("✉️ Написать", "✉️ Написати", "✉️ Message"),
    "report": ("⚠️ Пожаловаться", "⚠️ Поскаржитись", "⚠️ Report"),
    "thanks": ("Жалоба отправлена", "Скаргу надіслано", "Report sent"),
    "renew": ("🔄 Продлить", "🔄 Продовжити", "🔄 Renew"),
    "delete": ("🗑 Удалить", "🗑 Видалити", "🗑 Delete"),
    "done": ("Готово", "Готово", "Done"),
    "yours": ("Это ваша анкета", "Це ваша анкета", "This is your listing"),
    "hidden": ("(скрыта)", "(прихована)", "(hidden)"),
    "paused_tag": ("⏸ на паузе: нужно {p} 🪙 свободных", "⏸ на паузі: потрібно {p} 🪙 вільних", "⏸ paused: needs {p} 🪙 available"),
    "boosted_tag": ("🚀 в топе до {d}", "🚀 у топі до {d}", "🚀 on top until {d}"),
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
    "reason_ask": ("Укажите причину отказа одним сообщением: что именно не выполнено. Без причины отказ не отправится.\n\nПередумали — нажмите «Отменить»: заявка останется на проверке, кнопки под ней сохранятся.",
                   "Вкажіть причину відмови одним повідомленням: що саме не виконано. Без причини відмова не надішлеться.\n\nПередумали — натисніть «Відмінити»: заявка залишиться на перевірці, кнопки під нею збережуться.",
                   "Give the reason for declining in one message: what was not done. Without a reason the decline is not sent.\n\nChanged your mind? Tap “Cancel”: the claim stays open and its buttons remain."),
    "b_pm": ("🖼 Скринов: {n}", "🖼 Скрінів: {n}", "🖼 Screenshots: {n}"),
    "pm_set": ("Теперь нужно скриншотов: {n}", "Тепер потрібно скрінів: {n}", "Now screenshots required: {n}"),
    "b_price": ("💰 Оплата: {n} 🪙", "💰 Оплата: {n} 🪙", "💰 Pay: {n} 🪙"),
    "price_set": ("Теперь за задание вы платите {n} 🪙. Дороже — заметнее в каталоге, но монеты тратятся быстрее.",
                  "Тепер за завдання ви платите {n} 🪙. Дорожче — помітніше в каталозі, але монети витрачаються швидше.",
                  "You now pay {n} 🪙 per task. Higher pay stands out in the catalog, but coins run out faster."),
    "b_boost": ("🚀 Поднять в топ · {n} 🪙", "🚀 Підняти в топ · {n} 🪙", "🚀 Boost to top · {n} 🪙"),
    "boost_ok": ("🚀 Анкета поднята в топ каталога на {h} ч. Списано {n} 🪙.",
                 "🚀 Анкету піднято в топ каталогу на {h} год. Списано {n} 🪙.",
                 "🚀 Listing boosted to the top for {h} h. {n} 🪙 spent."),
    "boost_no": ("Не хватает свободных монет: нужно {n} 🪙, у вас {f}.", "Не вистачає вільних монет: потрібно {n} 🪙, у вас {f}.",
                 "Not enough available coins: {n} 🪙 needed, you have {f}."),
    "boost_off": ("Подъём анкет сейчас отключён.", "Підняття анкет зараз вимкнено.", "Boosting is turned off right now."),
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
    "tk_more": ("… ещё {n}", "… ще {n}", "… {n} more"),
    "b_cancel": ("❌ Отменить", "❌ Відмінити", "❌ Cancel"),
    "open_link": ("🔗 Открыть ссылку", "🔗 Відкрити посилання", "🔗 Open link"),
    "b_skip": ("⏭ Пропустить", "⏭ Пропустити", "⏭ Skip"),
    "need_text": ("Нужен текст. Напишите сообщением или нажмите «Отменить».", "Потрібен текст. Напишіть повідомленням або натисніть «Відмінити».",
                  "Text is needed. Write a message or tap “Cancel”."),
    "b_rating": ("👤 Профиль", "👤 Профіль", "👤 Profile"),
    "b_rules": ("⚖️ Правила", "⚖️ Правила", "⚖️ Rules"),
    "b_back": ("◀️ Назад", "◀️ Назад", "◀️ Back"),
    "b_what": ("❓ Как это работает", "❓ Як це працює", "❓ How it works"),
    "b_top": ("🥇 Топ", "🥇 Топ", "🥇 Top"),
    "b_accept": ("✅ Согласен", "✅ Погоджуюсь", "✅ I agree"),
    "claim": ("▶️ Выполнить · +{n} 🪙", "▶️ Виконати · +{n} 🪙", "▶️ Complete · +{n} 🪙"),
    "yes_btn": ("✅ Подтвердить", "✅ Підтвердити", "✅ Confirm"),
    "no_btn": ("❌ Не было", "❌ Не було", "❌ Not done"),
    "claim_ask": ("Пришлите скриншот регистрации по этой ссылке (фото). Без доказательства заявка не создаётся.",
                  "Надішліть скриншот реєстрації за цим посиланням (фото). Без доказу заявка не створюється.",
                  "Send a screenshot of your sign-up through this link (photo). Without proof the claim is not created."),
    "claim_need_photo": ("Нужен скриншот. Пришлите фото или /cancel.", "Потрібен скриншот. Надішліть фото або /cancel.",
                         "A screenshot is needed. Send a photo or /cancel."),
    "claim_sent": ("✅ Заявка отправлена автору. Для вас зарезервировано {p} 🪙. Если автор не ответит за {d} ч., задание засчитается автоматически.",
                   "✅ Заявку надіслано автору. Для вас зарезервовано {p} 🪙. Якщо автор не відповість за {d} год., завдання зарахується автоматично.",
                   "✅ Claim sent to the author. {p} 🪙 are reserved for you. If there is no answer in {d} hours, it counts automatically."),
    "claim_author": ("🔔 {who} утверждает, что выполнил задание на вашу анкету «{t}». Скриншот(ы) выше.\n\n🔒 Оплата {p} 🪙 зарезервирована. Проверьте и подтвердите. Без ответа за {d} ч. засчитаем автоматически.",
                     "🔔 {who} стверджує, що виконав завдання на вашу анкету «{t}». Скриншот(и) вище.\n\n🔒 Оплату {p} 🪙 зарезервовано. Перевірте і підтвердіть. Без відповіді за {d} год. зарахуємо автоматично.",
                     "🔔 {who} says they completed the task on your listing “{t}”. Screenshot(s) above.\n\n🔒 Payment of {p} 🪙 is reserved. Check and confirm. No answer in {d} hours and it counts automatically."),
    "claim_ok": ("✅ Задание по «{t}» засчитано: +{n} 🪙. Свободно: {f} 🪙.",
                 "✅ Завдання по «{t}» зараховано: +{n} 🪙. Вільно: {f} 🪙.",
                 "✅ Task on “{t}” counted: +{n} 🪙. Available: {f} 🪙."),
    "claim_paid": ("💸 −{n} 🪙: {who} выполнил задание по анкете «{t}»{auto}. Свободно: {f} 🪙.",
                   "💸 −{n} 🪙: {who} виконав завдання за анкетою «{t}»{auto}. Вільно: {f} 🪙.",
                   "💸 −{n} 🪙: {who} completed the task on your listing “{t}”{auto}. Available: {f} 🪙."),
    "auto_note": (" (засчитано автоматически)", " (зараховано автоматично)", " (counted automatically)"),
    "claim_fake": ("⚠️ Заявка по «{t}» признана фейком. Штраф −{n} 🪙.", "⚠️ Заявку по «{t}» визнано фейком. Штраф −{n} 🪙.",
                   "⚠️ The claim on “{t}” was ruled fake. Penalty −{n} 🪙."),
    "claim_refund": ("↩️ Заявка на «{t}» признана фейком. Резерв {n} 🪙 вернулся вам.",
                     "↩️ Заявку на «{t}» визнано фейком. Резерв {n} 🪙 повернувся вам.",
                     "↩️ The claim on “{t}” was ruled fake. The {n} 🪙 reserve is back to you."),
    "claim_expired": ("⌛ Спор по «{t}» закрыт без решения. Монеты никому не начислены, резерв вернулся автору.",
                      "⌛ Спір щодо «{t}» закрито без рішення. Монети нікому не нараховано, резерв повернувся автору.",
                      "⌛ The dispute on “{t}” closed without a decision. No coins moved, the reserve went back to the author."),
    "claim_disputed": ("⚖️ Автор возразил: «{r}». Спор рассмотрит администратор, мы сообщим результат.",
                       "⚖️ Автор заперечив: «{r}». Спір розгляне адміністратор, ми повідомимо результат.",
                       "⚖️ The author disputed it: “{r}”. An admin will review it and we will let you know."),
    "dispute_sent": ("Спор открыт. Администратор посмотрит доказательство. Резерв останется до решения.",
                     "Спір відкрито. Адміністратор перегляне доказ. Резерв залишиться до рішення.",
                     "Dispute opened. An admin will review the proof. The reserve stays until the decision."),
    "b_support": ("🆘 Поддержка", "🆘 Підтримка", "🆘 Support"),
    "skip": ("⏭ Уже зарегистрирован", "⏭ Вже зареєстрований", "⏭ Already registered"),
    "sup_ask": ("Опишите вопрос или предложение одним сообщением (текст или фото). /cancel — отмена.",
                "Опишіть питання або пропозицію одним повідомленням (текст або фото). /cancel — скасувати.",
                "Describe your question or suggestion in one message (text or photo). /cancel to stop."),
    "sup_reply": ("💬 <b>Ответ поддержки</b>\n\n{text}", "💬 <b>Відповідь підтримки</b>\n\n{text}",
                  "💬 <b>Support reply</b>\n\n{text}"),
    "adm_msg": ("💬 <b>Сообщение от администрации</b>\n\n{text}", "💬 <b>Повідомлення від адміністрації</b>\n\n{text}",
                "💬 <b>Message from the admins</b>\n\n{text}"),
    "adm_msg_media": ("💬 <b>Сообщение от администрации</b> ⬇️", "💬 <b>Повідомлення від адміністрації</b> ⬇️",
                      "💬 <b>Message from the admins</b> ⬇️"),
    "b_reply": ("✍️ Ответить", "✍️ Відповісти", "✍️ Reply"),
    "n_paused": ("⏸ <b>Ваши анкеты на паузе</b>\n\nСвободных монет: {f} 🪙. Этого не хватает, чтобы оплатить следующее задание, поэтому анкеты скрыты из каталога.\n\nВыполните задание из каталога: после подтверждения анкеты вернутся сами.",
                 "⏸ <b>Ваші анкети на паузі</b>\n\nВільних монет: {f} 🪙. Цього не вистачає, щоб оплатити наступне завдання, тому анкети приховано з каталогу.\n\nВиконайте завдання з каталогу: після підтвердження анкети повернуться самі.",
                 "⏸ <b>Your listings are paused</b>\n\nAvailable coins: {f} 🪙. That is not enough to pay for the next task, so your listings are hidden from the catalog.\n\nComplete a task from the catalog: once it is confirmed, they come back automatically."),
    "n_resumed": ("▶️ Свободных монет снова хватает ({f} 🪙). Ваши анкеты снова в каталоге.",
                  "▶️ Вільних монет знову достатньо ({f} 🪙). Ваші анкети знову в каталозі.",
                  "▶️ You have enough available coins again ({f} 🪙). Your listings are back in the catalog."),
    "n_low": ("🪙 Осталась {f} свободная монета. Когда она уйдёт в резерв, анкеты встанут на паузу.",
              "🪙 Залишилась {f} вільна монета. Коли вона піде в резерв, анкети стануть на паузу.",
              "🪙 {f} available coin left. Once it is reserved, your listings will pause."),
    "n_invite": ("🤝 Ваш друг выполнил задания разных авторов: +{n} 🪙.", "🤝 Ваш друг виконав завдання різних авторів: +{n} 🪙.",
                 "🤝 Your friend completed tasks for different authors: +{n} 🪙."),
    "n_admin_add": ("🎁 Администрация начислила вам +{n} 🪙.{note}\nСвободно: {f} 🪙.",
                    "🎁 Адміністрація нарахувала вам +{n} 🪙.{note}\nВільно: {f} 🪙.",
                    "🎁 The admins added +{n} 🪙 to your account.{note}\nAvailable: {f} 🪙."),
    "n_admin_sub": ("🪙 Администрация списала {n} 🪙.{note}\nСвободно: {f} 🪙.",
                    "🪙 Адміністрація списала {n} 🪙.{note}\nВільно: {f} 🪙.",
                    "🪙 The admins removed {n} 🪙 from your account.{note}\nAvailable: {f} 🪙."),
    "n_note": ("\nПричина: {r}", "\nПричина: {r}", "\nReason: {r}"),
    "n_start": ("🎁 Стартовый бонус: +{n} 🪙.", "🎁 Стартовий бонус: +{n} 🪙.", "🎁 Welcome bonus: +{n} 🪙."),
    "my_title": ("📋 <b>Мои анкеты</b>", "📋 <b>Мої анкети</b>", "📋 <b>My listings</b>"),
    "proof_dup": ("Этот скриншот уже использовали в другой заявке. Сделайте новый скриншот и пришлите его.",
                  "Цей скриншот уже використовували в іншій заявці. Зробіть новий скриншот і надішліть його.",
                  "This screenshot was already used in another claim. Take a new screenshot and send it."),
    "proof_same": ("Скриншоты ДО и ПОСЛЕ одинаковые. Пришлите скриншот ПОСЛЕ выполнения.",
                   "Скриншоти ДО і ПІСЛЯ однакові. Надішліть скриншот ПІСЛЯ виконання.",
                   "The BEFORE and AFTER screenshots are the same. Send the AFTER screenshot."),
    "sup_slow": ("Слишком много сообщений подряд. Подождите несколько минут: администратор уже видит ваш вопрос.",
                 "Забагато повідомлень поспіль. Зачекайте кілька хвилин: адміністратор уже бачить ваше питання.",
                 "Too many messages in a row. Wait a few minutes: an admin already sees your question."),
    "n_digest": ("🆕 За сутки в каталоге появилось новых анкет: <b>{n}</b>. За каждое выполненное задание вы получаете монеты 🪙.",
                 "🆕 За добу в каталозі з'явилося нових анкет: <b>{n}</b>. За кожне виконане завдання ви отримуєте монети 🪙.",
                 "🆕 New listings in the catalog in the last day: <b>{n}</b>. You earn coins 🪙 for each completed task."),
    "remind": ("⏰ Через 2 часа заявка на «{t}» засчитается автоматически и {p} 🪙 уйдут исполнителю. Проверьте её и подтвердите или отклоните.",
               "⏰ Через 2 години заявка на «{t}» зарахується автоматично і {p} 🪙 підуть виконавцю. Перевірте її і підтвердіть або відхиліть.",
               "⏰ In 2 hours the claim on “{t}” counts automatically and {p} 🪙 go to the executor. Check it and confirm or decline."),
    "dispute_limit": ("Лимит споров исчерпан ({n} из последних 10 заявок). Подтвердите заявку или дождитесь автозачёта.",
                      "Ліміт спорів вичерпано ({n} з останніх 10 заявок). Підтвердіть заявку або дочекайтесь автозарахування.",
                      "Dispute limit reached ({n} of the last 10 claims). Confirm the claim or wait for the automatic count."),
    "claim_done": ("Заявка уже рассмотрена.", "Заявку вже розглянуто.", "This claim is already decided."),
    "claim_gone": ("Анкета больше не доступна.", "Анкета більше не доступна.", "This listing is no longer available."),
    "claim_nocoins": ("У автора закончились монеты, анкета на паузе. Загляните позже.",
                      "В автора закінчилися монети, анкета на паузі. Загляньте пізніше.",
                      "The author is out of coins, the listing is paused. Check back later."),
    "claim_limit": ("Слишком много открытых заявок ({n}). Дождитесь решения по ним.",
                    "Забагато відкритих заявок ({n}). Дочекайтесь рішень по них.", "Too many open claims ({n}). Wait for the decisions."),
    "claim_pair": ("По этому автору заявка уже открыта. Дождитесь ответа.",
                   "По цьому автору заявка вже відкрита. Дочекайтесь відповіді.",
                   "A claim for this author is already open. Wait for the answer."),
    "claim_repeat": ("Вы уже выполняли это задание.", "Ви вже виконували це завдання.", "You have already done this task."),
    "claim_cool": ("Вы недавно выполняли задание этого автора. Следующее можно через {d} дн.",
                   "Ви нещодавно виконували завдання цього автора. Наступне можна через {d} дн.",
                   "You recently did a task for this author. The next one opens in {d} days."),
    "claim_newbie": ("Новые аккаунты могут подать не больше {n} заявок в сутки. Попробуйте завтра.",
                     "Нові акаунти можуть подати не більше {n} заявок на добу. Спробуйте завтра.",
                     "New accounts can send up to {n} claims a day. Try again tomorrow."),
    "no_coins_new": ("🔒 <b>Нужна свободная монета</b>\n\nЧтобы создать анкету, нужно хотя бы 1 🪙 свободных. Сейчас у вас: {f}.\n\nМонеты дают за чужие задания: откройте каталог, выполните задание, и после подтверждения автора монета ваша.",
                     "🔒 <b>Потрібна вільна монета</b>\n\nЩоб створити анкету, потрібно хоча б 1 🪙 вільних. Зараз у вас: {f}.\n\nМонети дають за чужі завдання: відкрийте каталог, виконайте завдання, і після підтвердження автора монета ваша.",
                     "🔒 <b>You need an available coin</b>\n\nTo create a listing you need at least 1 🪙 available. You have: {f}.\n\nYou earn coins by doing other people's tasks: open the catalog, complete a task, and the coin is yours once the author confirms."),
    "rules_text": ("⚖️ <b>Правила взаимных рефералов</b>\n\n1. Задание — реальная регистрация по ссылке автора. Фейки запрещены.\n2. За выполненное задание вы получаете монеты 🪙, за задания по вашим анкетам — платите. Без свободных монет анкета на паузе, а новую не создать.\n3. У автора {hours} ч. на ответ. Без ответа заявка засчитывается сама.\n4. Фейк, подтверждённый администратором: штраф −{pen} 🪙.\n5. Выполнять задания самому себе со второго аккаунта нельзя: за это бан.\n\nНажимая «Согласен», вы принимаете эти правила.",
                   "⚖️ <b>Правила взаємних рефералів</b>\n\n1. Завдання — реальна реєстрація за посиланням автора. Фейки заборонені.\n2. За виконане завдання ви отримуєте монети 🪙, за завдання за вашими анкетами — платите. Без вільних монет анкета на паузі, а нову не створити.\n3. Автор має {hours} год. на відповідь. Без відповіді заявка зараховується сама.\n4. Фейк, підтверджений адміністратором: штраф −{pen} 🪙.\n5. Виконувати завдання самому собі з другого акаунта не можна: за це бан.\n\nНатискаючи «Погоджуюсь», ви приймаєте ці правила.",
                   "⚖️ <b>Mutual referral rules</b>\n\n1. A task is a real sign-up through the author's link. Fakes are not allowed.\n2. You earn coins 🪙 for completed tasks and pay coins for tasks done on your listings. With no available coins your listing pauses and you can't create a new one.\n3. The author has {hours} hours to answer. No answer and the claim counts automatically.\n4. A fake confirmed by an admin: penalty −{pen} 🪙.\n5. Doing tasks for yourself from a second account is not allowed and leads to a ban.\n\nBy tapping “I agree” you accept these rules."),
    "rt_main": ("👤 <b>Ваш профиль</b>\n\n🪙 Монеты: <b>{c}</b> · свободно {f}\n🏆 Рейтинг (выполнено заданий): {r}\n👍 Подтверждаете работы: {p}\n⚠️ Фейков: {fk}",
                "👤 <b>Ваш профіль</b>\n\n🪙 Монети: <b>{c}</b> · вільно {f}\n🏆 Рейтинг (виконано завдань): {r}\n👍 Погоджуєте роботи: {p}\n⚠️ Фейків: {fk}",
                "👤 <b>Your profile</b>\n\n🪙 Coins: <b>{c}</b> · available {f}\n🏆 Rating (tasks completed): {r}\n👍 You approve: {p}\n⚠️ Fakes: {fk}"),
    "rt_what_text": ("❓ <b>Как это работает</b>\n\n🪙 Монеты — валюта бота. Подробнее в разделе «Монеты».\n🏆 Рейтинг — сколько заданий вы выполнили. Растёт только от честных заданий.\n👍 Процент — сколько заявок вы подтвердили, а не отклонили.",
                     "❓ <b>Як це працює</b>\n\n🪙 Монети — валюта бота. Детальніше в розділі «Монети».\n🏆 Рейтинг — скільки завдань ви виконали. Росте лише від чесних завдань.\n👍 Відсоток — скільки заявок ви погодили, а не відхилили.",
                     "❓ <b>How it works</b>\n\n🪙 Coins are the bot's currency. See the “Coins” section.\n🏆 Rating: how many tasks you completed. It grows only from honest tasks.\n👍 Percentage: how many claims you approved versus declined."),
    "coins_main": ("🪙 <b>Мои монеты</b>\n\nВсего: <b>{c}</b>\n🔒 В резерве под заявки: {h}\n✅ Свободно: <b>{f}</b>{warn}\n\n<b>Последние операции</b>\n{tx}",
                   "🪙 <b>Мої монети</b>\n\nУсього: <b>{c}</b>\n🔒 У резерві під заявки: {h}\n✅ Вільно: <b>{f}</b>{warn}\n\n<b>Останні операції</b>\n{tx}",
                   "🪙 <b>My coins</b>\n\nTotal: <b>{c}</b>\n🔒 Reserved for claims: {h}\n✅ Available: <b>{f}</b>{warn}\n\n<b>Recent activity</b>\n{tx}"),
    "coins_warn": ("\n\n⏸ Свободных монет нет: анкеты на паузе. Выполните задание из каталога.",
                   "\n\n⏸ Вільних монет немає: анкети на паузі. Виконайте завдання з каталогу.",
                   "\n\n⏸ No available coins: your listings are paused. Complete a task from the catalog."),
    "coins_how": ("❓ <b>Как работают монеты</b>\n\n🪙 Вы получаете монеты, когда выполняете чужие задания и автор их подтверждает (или заявка засчитывается автоматически).\n💸 Вы платите тем, кто выполнил ваше задание. Цену 1–{mx} 🪙 вы выбираете в «Мои анкеты».\n🔒 Когда на вашу анкету подают заявку, оплата резервируется. Если заявку признают фейком, монеты вернутся.\n⏸ Если свободных монет меньше цены, анкета на паузе и вернётся в каталог сама после пополнения.\n➕ Новую анкету можно создать, только если есть свободная монета.\n⚠️ За фейк — штраф −{pen} 🪙.{boost}{invite}{fee}",
                  "❓ <b>Як працюють монети</b>\n\n🪙 Ви отримуєте монети, коли виконуєте чужі завдання і автор їх підтверджує (або заявка зараховується автоматично).\n💸 Ви платите тим, хто виконав ваше завдання. Ціну 1–{mx} 🪙 ви обираєте в «Мої анкети».\n🔒 Коли на вашу анкету подають заявку, оплата резервується. Якщо заявку визнають фейком, монети повернуться.\n⏸ Якщо вільних монет менше за ціну, анкета на паузі й повернеться в каталог сама після поповнення.\n➕ Нову анкету можна створити, лише якщо є вільна монета.\n⚠️ За фейк — штраф −{pen} 🪙.{boost}{invite}{fee}",
                  "❓ <b>How coins work</b>\n\n🪙 You earn coins when you complete other people's tasks and the author confirms (or the claim counts automatically).\n💸 You pay those who complete your task. Set the price, 1–{mx} 🪙, in “My listings”.\n🔒 When someone claims your listing, the payment is reserved. If the claim is ruled fake, the coins come back.\n⏸ If your available coins are below the price, the listing pauses and returns to the catalog on its own after a top-up.\n➕ You can create a new listing only with an available coin.\n⚠️ A fake costs −{pen} 🪙.{boost}{invite}{fee}"),
    "how_boost": ("\n🚀 Монеты можно потратить на подъём анкеты в топ: {n} 🪙 за {h} ч.", "\n🚀 Монети можна витратити на підняття анкети в топ: {n} 🪙 за {h} год.",
                  "\n🚀 Spend coins to boost a listing to the top: {n} 🪙 for {h} h."),
    "how_invite": ("\n🤝 Пригласите друга: +{n} 🪙, когда его задания подтвердят другие авторы, кроме вас (нужно авторов: {k}).",
                   "\n🤝 Запросіть друга: +{n} 🪙, коли його завдання підтвердять інші автори, крім вас (потрібно авторів: {k}).",
                   "\n🤝 Invite a friend: +{n} 🪙 once authors other than you confirm their tasks ({k} authors needed)."),
    "how_fee": ("\n🏦 Комиссия бота: {n}% от оплаты, исполнитель всегда получает минимум 1 🪙.",
                "\n🏦 Комісія бота: {n}% від оплати, виконавець завжди отримує щонайменше 1 🪙.",
                "\n🏦 Bot fee: {n}% of the payment; the executor always gets at least 1 🪙."),
    "tx_none": ("Пока операций нет.", "Поки операцій немає.", "No activity yet."),
    "invite_text": ("🤝 <b>Пригласите друга</b>\n\n{bonus}Отправьте другу ссылку:\n<code>{link}</code>\n\nПриглашено: {a} · бонус получен: {b}",
                    "🤝 <b>Запросіть друга</b>\n\n{bonus}Надішліть другу посилання:\n<code>{link}</code>\n\nЗапрошено: {a} · бонус отримано: {b}",
                    "🤝 <b>Invite a friend</b>\n\n{bonus}Send your friend this link:\n<code>{link}</code>\n\nInvited: {a} · bonus earned: {b}"),
    "invite_bonus": ("Вы получите +{n} 🪙, когда задания друга подтвердят разные авторы, кроме вас (нужно авторов: {k}).\n\n",
                     "Ви отримаєте +{n} 🪙, коли завдання друга підтвердять різні автори, крім вас (потрібно авторів: {k}).\n\n",
                     "You get +{n} 🪙 when different authors, other than you, confirm your friend's tasks ({k} authors needed).\n\n"),
    "notif_head": ("🔔 <b>Уведомления</b>\n\nВыберите, что присылать. Заявки на ваши анкеты, напоминания о дедлайне, споры и сообщения администрации приходят всегда, иначе можно пропустить автозачёт.",
                   "🔔 <b>Сповіщення</b>\n\nОберіть, що надсилати. Заявки на ваші анкети, нагадування про дедлайн, спори й повідомлення адміністрації приходять завжди, інакше можна пропустити автозарахування.",
                   "🔔 <b>Notifications</b>\n\nChoose what to receive. Claims on your listings, deadline reminders, disputes and messages from the admins always arrive, or you could miss the automatic count."),
    "nt_claims": ("Результаты моих заявок", "Результати моїх заявок", "Results of my claims"),
    "nt_coins": ("Списания и остаток монет", "Списання і залишок монет", "Coin payments and balance"),
    "nt_listing": ("Пауза и срок анкет", "Пауза і термін анкет", "Listing pause and expiry"),
    "nt_news": ("Новости и рассылки", "Новини й розсилки", "News and announcements"),
    "nt_digest": ("Подборка новых анкет (раз в день)", "Добірка нових анкет (раз на день)", "New listings digest (daily)"),
    "rt_top_title": ("🥇 <b>Топ по рейтингу</b>\n\n", "🥇 <b>Топ за рейтингом</b>\n\n", "🥇 <b>Rating top</b>\n\n"),
    "rt_top_empty": ("Пока пусто.", "Поки порожньо.", "Nothing here yet."),
    "cat_head": ("🔎 <b>Каталог</b>\n───────────\nЧто ищем сегодня? За каждое задание вы получаете монеты 🪙.",
                 "🔎 <b>Каталог</b>\n───────────\nЩо шукаємо сьогодні? За кожне завдання ви отримуєте монети 🪙.",
                 "🔎 <b>Catalog</b>\n───────────\nWhat are we looking for today? Every task earns you coins 🪙."),
    "new_head": ("➕ <b>Новая анкета</b>\n───────────\nШаг 1 из 4: выберите категорию",
                 "➕ <b>Нова анкета</b>\n───────────\nКрок 1 з 4: оберіть категорію",
                 "➕ <b>New listing</b>\n───────────\nStep 1 of 4: choose a category"),
    "check": ("✅ Я подписался", "✅ Я підписався", "✅ I subscribed"),
    "hint": ("Если кнопка не открывается — автор скрыл профиль.", "Якщо кнопка не відкривається — автор сховав профіль.",
             "If the button doesn't open, the author hid their profile."),
    "pct_fmt": ("{p}% из {n} решений", "{p}% з {n} рішень", "{p}% of {n} decisions"),
    "pct_none": ("ещё нет решений", "ще немає рішень", "no decisions yet"),
    "card_info": ("💰 Оплата: <b>{n} 🪙</b>\n🏆 Рейтинг автора: {r} · 👍 Подтверждает: {p}\n📸 Скриншотов: {s}",
                  "💰 Оплата: <b>{n} 🪙</b>\n🏆 Рейтинг автора: {r} · 👍 Погоджує: {p}\n📸 Скрінів: {s}",
                  "💰 Pay: <b>{n} 🪙</b>\n🏆 Author rating: {r} · 👍 Approves: {p}\n📸 Screenshots: {s}"),
    "shots_1": ("1 (после выполнения)", "1 (після виконання)", "1 (after completing)"),
    "shots_2": ("2 (до запуска и после)", "2 (до запуску і після)", "2 (before and after)"),
}

# Coin transaction labels for the history screens
TX = {
    "earn": ("задание выполнено", "завдання виконано", "task completed"),
    "pay": ("оплата задания", "оплата завдання", "task payment"),
    "fake": ("штраф за фейк", "штраф за фейк", "fake penalty"),
    "admin": ("администрация", "адміністрація", "admins"),
    "boost": ("подъём анкеты", "підняття анкети", "listing boost"),
    "invite": ("бонус за друга", "бонус за друга", "friend bonus"),
    "start": ("стартовый бонус", "стартовий бонус", "welcome bonus"),
    "migrate": ("перенос баланса", "перенесення балансу", "balance transfer"),
}

# Notification categories a user can turn off. Everything else is critical and always delivered.
NOTIF_KEYS = ("claims", "coins", "listing", "news", "digest")


def t(lang, key, **kw):
    s = T[key][LANGS.index(lang)]
    return s.format(**kw) if kw else s


def tx_label(lang, kind):
    return TX.get(kind, (kind,) * 3)[LANGS.index(lang)]


# ---------- SETTINGS (cached for 60 s) ----------
DEFAULTS = {"ttl_days": "30", "report_threshold": "3", "sub_required": "0", "sub_channel": "",
            "welcome_ru": "", "welcome_uk": "", "welcome_en": "",
            "claim_hours": "12", "open_claims_max": "3", "pair_cooldown_days": "7",
            "penalty_per_strike": "3", "disputes_per_10": "5",
            "price_max": "3", "start_coins": "0", "fee_pct": "0", "boost_price": "3", "boost_hours": "24",
            "invite_bonus": "1", "invite_tasks": "3", "new_days": "3", "new_daily_claims": "5"}
NUM_KEYS = ("ttl_days", "report_threshold", "claim_hours", "open_claims_max", "pair_cooldown_days",
            "penalty_per_strike", "disputes_per_10", "price_max", "start_coins", "fee_pct", "boost_price",
            "boost_hours", "invite_bonus", "invite_tasks", "new_days", "new_daily_claims")
_set_cache, _set_ts = {}, 0.0


async def _load_settings():
    global _set_ts
    rows = await db.fetch("select key, value from settings")
    _set_cache.clear()
    _set_cache.update({x["key"]: x["value"] for x in rows})
    _set_ts = time.monotonic()


async def setting(k):
    if time.monotonic() - _set_ts > 60:
        await _load_settings()
    v = _set_cache.get(k)
    return DEFAULTS.get(k, "") if v is None else v


async def num(k):
    try:
        return int(await setting(k))
    except ValueError:
        return int(DEFAULTS.get(k, "0") or 0)


async def set_setting(k, v):
    await db.execute("insert into settings(key,value) values($1,$2) on conflict(key) do update set value=excluded.value", k, v)
    _set_cache[k] = v


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
        log.exception("subscription check failed (is the bot an admin of the channel?)")
        return None  # fail open, so a misconfigured channel does not lock everyone out


# ---------- FSM STORAGE IN NEON (survives Render restarts) ----------
class PgStorage(BaseStorage):
    """Keeps FSM states in Postgres with a write-through memory cache, so a restart does not lose a half-filled form."""

    def __init__(self):
        self._cache = {}
        self._used = {}  # key -> monotonic time of the last access, for pruning

    @staticmethod
    def _k(key):
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id or ''}:{key.destiny}"

    async def _row(self, k):
        self._used[k] = time.monotonic()
        if k not in self._cache:
            r = await db.fetchrow("select state, data::text data from fsm where k=$1", k)
            self._cache[k] = {"state": r["state"] if r else None, "data": json.loads(r["data"]) if r else {}}
        return self._cache[k]

    async def set_state(self, key, state=None):
        k = self._k(key)
        s = state.state if isinstance(state, State) else state
        row = await self._row(k)
        row["state"] = s
        await self._save(k, row)

    async def get_state(self, key):
        return (await self._row(self._k(key)))["state"]

    async def set_data(self, key, data):
        k = self._k(key)
        row = await self._row(k)
        row["data"] = dict(data)
        await self._save(k, row)

    async def get_data(self, key):
        return dict((await self._row(self._k(key)))["data"])

    async def _save(self, k, row):
        if row["state"] is None and not row["data"]:
            await db.execute("delete from fsm where k=$1", k)
            self._cache.pop(k, None)
            self._used.pop(k, None)
            return
        await db.execute("""insert into fsm(k, state, data, updated) values($1,$2,$3::jsonb,now())
            on conflict(k) do update set state=excluded.state, data=excluded.data, updated=now()""",
                         k, row["state"], json.dumps(row["data"], ensure_ascii=False))

    def prune(self, idle=3600):
        """Drops entries nobody touched for an hour. Safe: every write already went to Postgres."""
        edge = time.monotonic() - idle
        for k in [k for k, ts in self._used.items() if ts < edge]:
            self._cache.pop(k, None)
            self._used.pop(k, None)

    async def close(self):
        self._cache.clear()
        self._used.clear()


storage = PgStorage()


# ---------- DB: numbered migrations ----------
M1_BASE = """
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

# Coins replace the old balance. Old positive balance becomes coins, debt is forgiven.
M2_COINS = """
alter table users add column if not exists coins int not null default 0;
alter table users add column if not exists notif_off text[] not null default '{}';
alter table users add column if not exists invited_by bigint;
alter table users add column if not exists invite_paid boolean not null default false;
alter table users add column if not exists digest_at timestamptz;
alter table listings add column if not exists price int not null default 1;
alter table listings add column if not exists boosted_until timestamptz;
alter table claims add column if not exists price int not null default 1;
alter table claims add column if not exists reward int;
create table if not exists coin_tx(id bigserial primary key, uid bigint not null, delta int not null, kind text not null,
  claim_id bigint, admin_id bigint, note text, created timestamptz not null default now());
create index if not exists coin_tx_uid on coin_tx(uid, id desc);
create index if not exists claims_receiver on claims(receiver, status);
create index if not exists claims_listing on claims(listing_id, giver);
create index if not exists users_invited on users(invited_by);
create table if not exists fsm(k text primary key, state text, data jsonb not null default '{}',
  updated timestamptz not null default now());
update users set coins = greatest(balance, 0);
insert into coin_tx(uid, delta, kind) select id, coins, 'migrate' from users where coins > 0;
update claims set status='expired', decided=now() where id in (
  select id from (select id, row_number() over (partition by giver, receiver order by id) rn
                  from claims where status in ('pending','disputed')) s where rn > 1);
create unique index if not exists claims_open_pair on claims(giver, receiver) where status in ('pending','disputed');
create or replace function free_coins(p_uid bigint) returns int language sql stable as $fn$
  select coalesce((select coins from users where id = p_uid), 0)
       - coalesce((select sum(price) from claims where receiver = p_uid and status in ('pending','disputed')), 0)::int
$fn$;
update users u set locked = coalesce(free_coins(u.id) < (select min(price) from listings l
  where l.owner = u.id and l.status = 'active'), false);
"""

# Duplicate-screenshot check and broadcasts that survive a restart.
M3_SAFETY = """
alter table claims add column if not exists proof_uid text;
alter table claims add column if not exists proof_before_uid text;
create index if not exists claims_proof_uid on claims(proof_uid) where proof_uid is not null;
create index if not exists claims_proof_before_uid on claims(proof_before_uid) where proof_before_uid is not null;
create table if not exists broadcasts(id bigserial primary key, admin_id bigint not null, text text not null,
  last_uid bigint not null default 0, ok int not null default 0, total int not null default 0,
  status text not null default 'running', runner text, heartbeat timestamptz,
  created timestamptz not null default now(), finished timestamptz);
"""

MIGRATIONS = [M1_BASE, M2_COINS, M3_SAFETY]


async def migrate():
    """Applies new migrations in one transaction. The advisory lock keeps two Render instances from racing on deploy."""
    async with db.acquire() as con:
        async with con.transaction():
            await con.execute("select pg_advisory_xact_lock(7314001)")
            await con.execute("create table if not exists schema_version(v int primary key, applied timestamptz not null default now())")
            v = await con.fetchval("select coalesce(max(v), 0) from schema_version")
            for i, sql in enumerate(MIGRATIONS, 1):
                if i > v:
                    await con.execute(sql)
                    await con.execute("insert into schema_version(v) values($1)", i)
                    log.info("migration %s applied", i)


# Listing is visible in the catalog when it is active, the owner is not banned and has enough free coins.
VISIBLE = "l.status='active' and not u.banned and free_coins(l.owner) >= l.price"


async def ban_user(uid):
    """Bans, removes listings and cancels open claims on both sides (reserves go back to the authors).
    Listings are marked deleted instead of being erased, so other users' task history and the coin journal stay intact."""
    await db.execute("insert into users(id, banned) values($1, true) on conflict(id) do update set banned=true", uid)
    await db.execute("update claims set status='rejected', decided=now() where giver=$1 and status in ('pending','disputed')", uid)
    await db.execute("update claims set status='expired', decided=now() where receiver=$1 and status in ('pending','disputed')", uid)
    await db.execute("update listings set status='deleted' where owner=$1", uid)
    await purge_deleted()
    _seen.pop(uid, None)


async def log_event(uid, kind):
    await db.execute("insert into events(uid, kind) values($1, $2)", uid, kind)


async def cat_counts():
    rows = await db.fetch(f"select l.category, count(*) n from listings l join users u on u.id=l.owner where {VISIBLE} group by l.category")
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


# ---------- COINS ----------
async def free_coins(uid, con=None):
    return await (con or db).fetchval("select free_coins($1)", uid) or 0


async def add_coins(con, uid, delta, kind, claim_id=None, admin_id=None, note=None):
    """Changes coins and writes the journal entry. Pass a connection to keep it inside a transaction."""
    await con.execute("update users set coins = coins + $2 where id=$1", uid, delta)
    await con.execute("insert into coin_tx(uid, delta, kind, claim_id, admin_id, note) values($1,$2,$3,$4,$5,$6)",
                      uid, delta, kind, claim_id, admin_id, note)


async def invite_due(con, uid, need):
    """Returns the inviter once the friend's tasks were confirmed by `need` different authors.
    The inviter and other people the inviter brought in do not count, so a ring of own accounts can't mint coins."""
    return await con.fetchval("""select u.invited_by from users u join users i on i.id=u.invited_by and not i.banned
        where u.id=$1 and not u.invite_paid and (
          select count(distinct c.receiver) from claims c join users r on r.id=c.receiver
          where c.giver=u.id and c.status='confirmed' and c.receiver<>u.invited_by
            and r.invited_by is distinct from u.invited_by) >= $2""", uid, need)


def calc_reward(price, fee_pct):
    """What the executor receives. The fee is rounded, but the executor always gets at least 1 coin."""
    if fee_pct <= 0:
        return price
    return max(1, price - (price * fee_pct + 50) // 100)


# ---------- NOTIFICATIONS ----------
async def user_lang(uid):
    lang = await db.fetchval("select lang from users where id=$1", uid)
    return lang if lang in LANGS else "ru"


async def notify(bot, uid, key, cat=None, kb=None, img=None, **kw):
    """Sends a localized message. cat is the notification category the user may have turned off; None = always."""
    u = await db.fetchrow("select lang, notif_off, banned from users where id=$1", uid)
    if not u or u["banned"]:
        return False
    if cat and cat in (u["notif_off"] or []):
        return False
    lang = u["lang"] if u["lang"] in LANGS else "ru"
    try:
        if img:
            await send_screen(bot, uid, img, t(lang, key, **kw), kb)
        else:
            await bot.send_message(uid, t(lang, key, **kw), reply_markup=kb)
        return True
    except TelegramForbiddenError:
        log.info("user %s blocked the bot", uid)
    except Exception as e:
        log.warning("notify %s to %s failed: %s", key, uid, e)
    return False


async def refresh_owner(uid, bot=None):
    """Tracks whether all of the owner's listings are paused (not enough free coins) and tells the owner once per change."""
    r = await db.fetchrow("""select u.locked, free_coins(u.id) f,
        (select min(price) from listings l where l.owner=u.id and l.status='active') minp
        from users u where u.id=$1""", uid)
    if not r:
        return
    paused = r["minp"] is not None and r["f"] < r["minp"]
    if paused == r["locked"]:
        return
    await db.execute("update users set locked=$2 where id=$1", uid, paused)
    if bot is None or r["minp"] is None:  # no active listings: nothing to tell the owner
        return
    if paused:
        await notify(bot, uid, "n_paused", cat="listing", img="paused", f=r["f"],
                     kb=KB(inline_keyboard=[[B(text=t(await user_lang(uid), "b_cat"), callback_data="cat")]]))
    else:
        await notify(bot, uid, "n_resumed", cat="listing", f=r["f"])


# ---------- CLAIMS: reserve, confirm, reject ----------
class _NoCoins(Exception):
    pass


async def who_label(uid):
    name = await db.fetchval("select username from users where id=$1", uid)
    return f"@{esc(name)}" if name else f"<code>{uid}</code>"


async def claim_block_reason(uid, x, lang):
    """Why this user can't claim listing x right now (None = allowed). Also the anti-farm checks."""
    r = await db.fetchrow("""select
        exists(select 1 from claims where giver=$1 and receiver=$2 and status in ('pending','disputed')) pair,
        exists(select 1 from claims where giver=$1 and listing_id=$3 and status <> 'expired') rep,
        (select count(*) from claims where giver=$1 and status in ('pending','disputed')) open_n,
        (select max(decided) from claims where giver=$1 and receiver=$2 and status='confirmed') last_ok,
        (select created from users where id=$1) ucreated,
        (select count(*) from claims where giver=$1 and created > now() - interval '1 day') day_n""",
                          uid, x["owner"], x["id"])
    if r["pair"]:
        return t(lang, "claim_pair")
    if r["rep"]:
        return t(lang, "claim_repeat")
    mx = await num("open_claims_max")
    if mx and r["open_n"] >= mx:
        return t(lang, "claim_limit", n=mx)
    cd = await num("pair_cooldown_days")
    now = datetime.now(timezone.utc)
    if cd and r["last_ok"] and r["last_ok"] > now - timedelta(days=cd):
        left = (r["last_ok"] + timedelta(days=cd) - now).total_seconds() / 86400
        return t(lang, "claim_cool", d=max(1, int(left + 0.999)))
    nd, nl = await num("new_days"), await num("new_daily_claims")
    if nl and r["ucreated"] and r["ucreated"] > now - timedelta(days=nd) and r["day_n"] >= nl:
        return t(lang, "claim_newbie", n=nl)
    if await free_coins(x["owner"]) < x["price"]:
        return t(lang, "claim_nocoins")
    return None


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
        """select c.id, c.giver, c.receiver, c.proof, c.reason, c.price, l.id lid, l.title
           from claims c join listings l on l.id=c.listing_id where c.id=$1""", cid)


async def owner_state(uid, bot):
    """Refreshes the pause state and warns when only one free coin is left."""
    await refresh_owner(uid, bot)
    r = await db.fetchrow("select locked, free_coins(id) f from users where id=$1", uid)
    if r and not r["locked"] and r["f"] == 1 and await db.fetchval(
            "select exists(select 1 from listings where owner=$1 and status='active')", uid):
        await notify(bot, uid, "n_low", cat="coins", f=1)


async def apply_confirmed(bot, cid, auto=False, forced=False):
    """Pays for a task: the author's reserved coins go to the executor (minus the bot fee).
    Guarded by status, so a double tap or a race cannot pay twice. forced = decided by an admin in a dispute."""
    fee, bonus, need = await num("fee_pct"), await num("invite_bonus"), max(1, await num("invite_tasks"))
    inviter = None
    async with db.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow("""update claims set status='confirmed', decided=now(), auto=$2
                where id=$1 and status in ('pending','disputed') returning giver, receiver, price, listing_id""", cid, auto)
            if not row:
                return False
            g, rc, price = row["giver"], row["receiver"], row["price"]
            reward = calc_reward(price, fee)
            title = await con.fetchval("select title from listings where id=$1", row["listing_id"]) or "—"
            await con.execute("update claims set reward=$2 where id=$1", cid, reward)
            await add_coins(con, rc, -price, "pay", cid)
            await add_coins(con, g, reward, "earn", cid)
            await con.execute("update users set rating=rating+1 where id=$1", g)
            if bonus > 0:
                inviter = await invite_due(con, g, need)
                # the guarded update makes sure the bonus is paid once, even if two confirmations race
                if inviter and await con.fetchval(
                        "update users set invite_paid=true where id=$1 and not invite_paid returning id", g):
                    await add_coins(con, inviter, bonus, "invite", cid)
                else:
                    inviter = None
    await notify(bot, g, "claim_ok", cat=None if forced else "claims", t=esc(title), n=reward, f=await free_coins(g))
    lang_rc = await user_lang(rc)
    await notify(bot, rc, "claim_paid", cat=None if forced else "coins", n=price, who=await who_label(g), t=esc(title),
                 auto=t(lang_rc, "auto_note") if auto else "", f=await free_coins(rc))
    if inviter:
        await notify(bot, inviter, "n_invite", cat="coins", n=bonus)
        await refresh_owner(inviter, bot)
    await refresh_owner(g, bot)
    await owner_state(rc, bot)
    return True


async def reject_claim(bot, cid):
    """An admin rules the claim fake: the reserve goes back to the author, the executor pays a penalty."""
    per = await num("penalty_per_strike")
    async with db.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow("""update claims set status='rejected', decided=now()
                where id=$1 and status in ('pending','disputed') returning giver, receiver, price, listing_id""", cid)
            if not row:
                return False
            title = await con.fetchval("select title from listings where id=$1", row["listing_id"]) or "—"
            await con.execute("update users set fakes=fakes+1 where id=$1", row["giver"])
            if per:
                await add_coins(con, row["giver"], -per, "fake", cid)
    await notify(bot, row["giver"], "claim_fake", t=esc(title), n=per)
    await notify(bot, row["receiver"], "claim_refund", t=esc(title), n=row["price"])
    await refresh_owner(row["giver"], bot)
    await refresh_owner(row["receiver"], bot)
    return True


async def send_claim_to_author(bot, cid):
    card = await db.fetchrow("""select c.id, c.giver, c.receiver, c.proof, c.mid, c.proof_before, c.mid_before, c.price, l.title
        from claims c join listings l on l.id=c.listing_id where c.id=$1""", cid)
    lang = await user_lang(card["receiver"])
    who = await who_label(card["giver"])
    if "@" in who:
        who += f" (<code>{card['giver']}</code>)"
    try:
        if card["proof_before"]:
            await forward_proof(bot, card["receiver"], card["giver"], card["mid_before"], card["proof_before"])
        await forward_proof(bot, card["receiver"], card["giver"], card["mid"], card["proof"])
        kb = KB(inline_keyboard=[[B(text=t(lang, "yes_btn"), callback_data=f"cf:{cid}"),
                                  B(text=t(lang, "no_btn"), callback_data=f"cd:{cid}")]])
        await bot.send_message(card["receiver"], t(lang, "claim_author", who=who, t=esc(card["title"]), p=card["price"],
                                                   d=await setting("claim_hours")), reply_markup=kb)
    except Exception:
        log.exception("could not send the claim to the author")


def dispute_caption(card):
    return (f"⚖️ Спір #{card['id']} · {card['price']} {COIN}\nЗаявник: <code>{card['giver']}</code>\nАвтор: <code>{card['receiver']}</code>\n"
            f"Анкета #{card['lid']} «{esc(card['title'])}»\nПричина автора: {esc(card['reason'] or '—')}\n"
            f"Докази заявника — вище.")


def dispute_kb(card):
    return KB(inline_keyboard=[
        [B(text="✅ Зарахувати", callback_data=f"dv:{card['id']}:ok"),
         B(text="❌ Фейк", callback_data=f"dv:{card['id']}:fake")],
        [B(text="✉️ Заявнику", callback_data=f"chw:{card['giver']}"),
         B(text="✉️ Автору", callback_data=f"chw:{card['receiver']}")],
        [B(text="👤 Заявник", callback_data=f"uc:{card['giver']}"),
         B(text="👤 Автор", callback_data=f"uc:{card['receiver']}")]])


async def notify_admins_dispute(bot, cid):
    card = await claim_card(cid)
    for a in ADMIN_IDS:
        try:
            await bot.send_photo(a, card["proof"], caption=dispute_caption(card), reply_markup=dispute_kb(card))
        except Exception:
            log.exception("could not send the dispute to admin")


async def claims_expiry(bot):
    """Reminds the author two hours before the deadline, auto-counts unanswered claims,
    closes disputes nobody decided within 7 days (the reserve goes back to the author). Runs hourly."""
    try:
        hours = await num("claim_hours")
        if hours > 2:
            for x in await db.fetch("""select c.id, c.receiver, c.price, l.title from claims c join listings l on l.id=c.listing_id
                    where c.status='pending' and not c.reminded and c.created < now() - make_interval(hours => $1)""", hours - 2):
                await db.execute("update claims set reminded=true where id=$1", x["id"])
                await notify(bot, x["receiver"], "remind", t=esc(x["title"]), p=x["price"])
        for x in await db.fetch("""select c.id from claims c where c.status='pending' and c.created < now() - make_interval(hours => $1)
                and not exists (select 1 from fsm f where f.state=$2 and f.data->>'cid' = c.id::text
                                and f.updated > now() - interval '30 minutes')""", hours, Reason.text.state):
            await apply_confirmed(bot, x["id"], auto=True)
        for x in await db.fetch("""update claims c set status='expired', decided=now()
                where status='disputed' and disputed_at < now() - interval '7 days'
                returning c.giver, c.receiver, (select title from listings where id=c.listing_id) title"""):
            for uid in (x["giver"], x["receiver"]):
                await notify(bot, uid, "claim_expired", t=esc(x["title"] or "—"))
            await refresh_owner(x["receiver"], bot)
    except Exception:
        log.exception("claims expiry failed")


async def rules_caption(lang):
    return t(lang, "rules_text", hours=await setting("claim_hours"), pen=await setting("penalty_per_strike"))


async def rating_text(uid, lang):
    u = await db.fetchrow("select coins, rating, fakes, free_coins(id) f from users where id=$1", uid)
    s = await owner_stats(uid)
    pct = t(lang, "pct_fmt", p=s["pct"], n=s["total"]) if s["total"] else t(lang, "pct_none")
    return t(lang, "rt_main", c=u["coins"], f=u["f"], r=u["rating"], p=pct, fk=u["fakes"])


def rating_kb(lang):
    return KB(inline_keyboard=[
        [B(text=t(lang, "b_what"), callback_data="rt:what"), B(text=t(lang, "b_top"), callback_data="rt:top")],
        [B(text=t(lang, "b_tasks"), callback_data="tasks"), B(text=t(lang, "b_rules"), callback_data="rules:view")],
        [B(text=t(lang, "b_coins"), callback_data="coins"), B(text=t(lang, "b_invite"), callback_data="inv")],
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
    rows = [[b("b_cat", "cat")], [b("b_new", "new"), b("b_my", "my")], [b("b_coins", "coins"), b("b_rating", "rt")],
            [b("b_notif", "nt"), b("b_lang", "lang")], [b("b_support", "sup")]]
    if is_admin:
        rows.append([b("b_admin", "adm")])
    return KB(inline_keyboard=rows)


# ---------- ILLUSTRATIONS ----------
ROOT_DIR = Path(__file__).parent
IMG_DIRS = [ROOT_DIR / "img", ROOT_DIR]  # img/ first; images next to main.py keep working
_file_ids = {}


def find_img(name):
    for d in IMG_DIRS:
        p = d / f"{name}.png"
        if p.exists():
            return p
    return None


async def send_screen(bot, chat_id, name, text, kb=None):
    """Sends a message with an illustration on top (<name>.png). Falls back to plain text."""
    path = find_img(name)
    if path and len(text) <= 1024:
        fid = _file_ids.get(name)
        try:
            msg = await bot.send_photo(chat_id, fid or FSInputFile(path), caption=text, reply_markup=kb)
            if not fid:
                _file_ids[name] = msg.photo[-1].file_id
            return msg
        except TelegramForbiddenError:
            raise
        except Exception:
            log.exception("send_photo failed, falling back to text")
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
            if not fid and res is not True:
                _file_ids[name] = res.photo[-1].file_id
            return res
        if not path and fits and msg.photo:
            return await msg.edit_caption(caption=text, reply_markup=kb)
        if not path and not msg.photo:
            return await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return msg
        log.warning("in-place edit failed, sending new message: %s", e)
    res = await send_screen(event.bot, msg.chat.id, name, text, kb)
    try:
        await msg.delete()
    except Exception:
        pass
    return res


def home_kb():
    return KB(inline_keyboard=[[B(text="🏠", callback_data="menu")]])


# ---------- MIDDLEWARE ----------
_seen = {}  # uid -> (lang, banned, monotonic time of the last DB write, username)
SEEN_TTL = 300  # last_seen is written at most once per 5 minutes per user


class Mw(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = event.from_user
        if u is None:
            return await handler(event, data)
        now = time.monotonic()
        start_bonus = 0
        c = _seen.get(u.id)
        if c is None or now - c[2] > SEEN_TTL or c[3] != u.username:
            guess = (u.language_code or "")[:2]
            guess = guess if guess in LANGS else None
            ref = None
            if isinstance(event, Message) and (event.text or "").startswith("/start ref_"):
                tail = event.text.split("ref_", 1)[1].strip()
                ref = int(tail) if tail.isdigit() and int(tail) != u.id else None
            row = await db.fetchrow(
                """insert into users(id,lang,username,last_seen,invited_by)
                   values($1,$2,$3,now(),(select id from users where id=$4 and not banned))
                   on conflict(id) do update set username=excluded.username, last_seen=now()
                   returning lang, banned, (xmax = 0) inserted""",
                u.id, guess, u.username, ref)
            if row["inserted"]:
                start = await num("start_coins")
                if start > 0:
                    async with db.acquire() as con:
                        async with con.transaction():
                            await add_coins(con, u.id, start, "start")
                    # with a known language, tell about the bonus right after the welcome screen;
                    # otherwise lang_set does it once the language is chosen
                    start_bonus = start if row["lang"] else 0
            c = (row["lang"], row["banned"], now, u.username)
            _seen[u.id] = c
        is_cb = isinstance(event, CallbackQuery)
        if c[1]:
            if is_cb:  # answer silently, so the button does not keep spinning
                try:
                    await event.answer()
                except Exception:
                    pass
            return
        msg = event.message if is_cb else event
        if c[0] is None and not (is_cb and (event.data or "").startswith("sl:")):
            await send_screen(data["bot"], msg.chat.id, "lang", t("ru", "choose"), lang_kb())
            if is_cb:
                await event.answer()
            return
        data["lang"] = c[0] or "ru"
        result = await handler(event, data)
        if start_bonus:
            await notify(data["bot"], u.id, "n_start", n=start_bonus)
        return result


def prune_caches():
    """Hourly: forget users idle for an hour. Their next update simply reloads them from the database."""
    edge = time.monotonic() - 3600
    for uid in [uid for uid, c in _seen.items() if c[2] < edge]:
        _seen.pop(uid, None)
    storage.prune()


def seen_set(uid, lang=None, banned=None):
    """Keeps the middleware cache in sync after a language change or (un)ban."""
    c = _seen.get(uid)
    if c:
        _seen[uid] = (lang if lang is not None else c[0], banned if banned is not None else c[1], c[2], c[3])


# ---------- STATES ----------
class New(StatesGroup):
    title = State()
    desc = State()
    link = State()


class Adm(StatesGroup):
    val = State()
    bc = State()
    chat = State()
    find = State()
    coins = State()


class Claim(StatesGroup):
    before = State()
    after = State()
    proof = State()


class Reason(StatesGroup):
    text = State()


class Support(StatesGroup):
    write = State()


# ---------- USER ROUTER ----------
r = Router()


async def send_menu(target, lang, text=None, img="welcome"):
    uid = target.from_user.id
    body = t(lang, "menu", f=await free_coins(uid))
    if text:
        body = f"{text}\n\n{body}"
    await screen(target, img, body, menu_kb(lang, uid in ADMIN_IDS))


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
    lang, uid = c.data[3:], c.from_user.id
    if lang in LANGS:
        first = await db.fetchval("select lang is null from users where id=$1", uid)
        await db.execute("update users set lang=$2 where id=$1", uid, lang)
        seen_set(uid, lang=lang)
        await send_menu(c, lang)
        if first:
            bonus = await db.fetchval("select delta from coin_tx where uid=$1 and kind='start' order by id limit 1", uid)
            if bonus:
                await notify(c.bot, uid, "n_start", n=bonus)
    await c.answer()


# --- create listing (needs at least one free coin) ---
def no_coins_kb(lang):
    return KB(inline_keyboard=[[B(text=t(lang, "b_cat"), callback_data="cat")],
                               [B(text=t(lang, "b_coins"), callback_data="coins"), B(text="🏠", callback_data="menu")]])


@r.callback_query(F.data == "new")
async def new(c: CallbackQuery, lang, state: FSMContext):
    await state.clear()
    f = await free_coins(c.from_user.id)
    if f < 1:
        await screen(c, "nocoins", t(lang, "no_coins_new", f=f), no_coins_kb(lang))
        return await c.answer()
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
    if "cat" not in d or "title" not in d:
        await state.clear()
        return await send_menu(m, lang)
    f = await free_coins(m.from_user.id)
    if f < 1:  # coins could have gone into a reserve while the form was being filled
        await state.clear()
        return await send_screen(m.bot, m.chat.id, "nocoins", t(lang, "no_coins_new", f=f), no_coins_kb(lang))
    await db.execute("insert into listings(owner,category,title,descr,link,price) values($1,$2,$3,$4,$5,1)",
                     m.from_user.id, d["cat"], d["title"], d.get("desc", ""), link)
    await log_event(m.from_user.id, "create")
    await refresh_owner(m.from_user.id)
    await state.clear()
    await send_menu(m, lang, t(lang, "saved", p=1), "saved")


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
    cond, args = VISIBLE, []
    if cat_ != "all":
        args.append(cat_)
        cond += f" and l.category=${len(args)}"
    args.append(c.from_user.id)
    n_uid = len(args)
    cond += (f" and not exists (select 1 from skips s where s.uid=${n_uid} and s.listing_id=l.id)"
             f" and not exists (select 1 from claims cl where cl.giver=${n_uid} and cl.listing_id=l.id and cl.status <> 'expired')")
    n = await db.fetchval(f"select count(*) from listings l join users u on u.id=l.owner where {cond}", *args)
    if not n:
        await screen(c, "empty", t(lang, "empty"), home_kb())
        return await c.answer()
    p = max(0, min(int(p), n - 1))
    x = await db.fetchrow(
        f"""select l.*, u.username, u.rating, coalesce(l.boosted_until > now(), false) boosted
            from listings l join users u on u.id=l.owner where {cond}
            order by boosted desc, l.price desc, l.updated desc, l.id offset {p} limit 1""", *args)
    if not x:  # the listing disappeared between the two queries
        if p:
            return await show_page(c, lang, cat_, 0)
        await screen(c, "empty", t(lang, "empty"), home_kb())
        return await c.answer()
    await log_event(c.from_user.id, "view")
    s = await owner_stats(x["owner"])
    pct = t(lang, "pct_fmt", p=s["pct"], n=s["total"]) if s["total"] else t(lang, "pct_none")
    shots = t(lang, "shots_2") if x["proof_mode"] == 2 else t(lang, "shots_1")
    reward = calc_reward(x["price"], await num("fee_pct"))
    info = t(lang, "card_info", n=reward, r=x["rating"], p=pct, s=shots)
    text = f"{'🚀 ' if x['boosted'] else ''}{t(lang, 'c_' + x['category'])}\n<b>{esc(x['title'])}</b>"
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
                 [B(text=t(lang, "claim", n=reward), callback_data=f"cl:{x['id']}")],
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
    await db.execute("insert into skips(uid, listing_id) select $1, id from listings where id=$2 on conflict do nothing",
                     c.from_user.id, int(lid))
    await show_page(c, lang, cat_, int(p))


@r.callback_query(F.data.startswith("rp:"))
async def report(c: CallbackQuery, lang):
    lid = int(c.data[3:])
    owner = await db.fetchval("select owner from listings where id=$1 and status<>'deleted'", lid)
    if owner is None:
        return await c.answer(t(lang, "claim_gone"), show_alert=True)
    if owner == c.from_user.id:
        return await c.answer(t(lang, "yours"), show_alert=True)
    await db.execute("insert into reports(listing_id,reporter) values($1,$2) on conflict do nothing", lid, c.from_user.id)
    await log_event(c.from_user.id, "report")
    # Only trusted reporters hide a listing: older than "new_days" or with at least one confirmed task.
    # Reports from fresh accounts are still stored and visible to admins.
    cnt = await db.fetchval("""select count(*) from reports rp join users u on u.id=rp.reporter
        where rp.listing_id=$1 and not u.banned and (u.rating > 0 or u.created < now() - make_interval(days => $2))""",
                            lid, await num("new_days"))
    if cnt >= await num("report_threshold"):
        hid = await db.fetchval("update listings set status='hidden' where id=$1 and status='active' returning id", lid)
        if hid:
            for a in ADMIN_IDS:
                try:
                    await c.bot.send_message(a, f"⚠️ Анкету #{lid} приховано після {cnt} скарг. /admin → Скарги")
                except Exception as e:
                    log.warning("admin %s not reachable: %s", a, e)
    await c.answer(t(lang, "thanks"), show_alert=True)


# --- my listings: one screen with paging; renew, delete, price, screenshots, boost ---
async def listing_view(x, lang, free, p=0, n=1):
    st = []
    if x["status"] == "hidden":
        st.append(t(lang, "hidden"))
    elif free < x["price"]:
        st.append(t(lang, "paused_tag", p=x["price"]))
    if x["boosted_until"] and x["boosted_until"] > datetime.now(timezone.utc):
        st.append(t(lang, "boosted_tag", d=f"{x['boosted_until']:%d.%m %H:%M} UTC"))
    status = ("\n" + " · ".join(st)) if st else ""
    shown = x["link"] if len(x["link"]) <= 60 else x["link"][:59] + "…"
    descr = f"{esc(x['descr'])}\n" if x["descr"] else ""
    text = (f"{t(lang, 'my_title')} · {p + 1}/{n}\n\n"
            f"{t(lang, 'c_' + x['category'])}{status}\n<b>{esc(x['title'])}</b>\n{descr}"
            f"🔗 <a href='{esc(x['link'])}'>{esc(shown)}</a>")
    rows = [[B(text=t(lang, "renew"), callback_data=f"lr:{x['id']}:m"), B(text=t(lang, "delete"), callback_data=f"ld:{x['id']}")],
            [B(text=t(lang, "open_link"), url=x["link"])],
            [B(text=t(lang, "b_price", n=x["price"]), callback_data=f"lp:{x['id']}"),
             B(text=t(lang, "b_pm", n=x["proof_mode"]), callback_data=f"pm:{x['id']}")]]
    bp = await num("boost_price")
    if bp > 0:
        rows.append([B(text=t(lang, "b_boost", n=bp), callback_data=f"lb:{x['id']}")])
    if n > 1:
        nav = []
        if p > 0:
            nav.append(B(text="◀️", callback_data=f"my:{p - 1}"))
        nav.append(B(text=f"{p + 1}/{n}", callback_data="noop"))
        if p < n - 1:
            nav.append(B(text="▶️", callback_data=f"my:{p + 1}"))
        rows.append(nav)
    rows.append([B(text=t(lang, "b_new"), callback_data="new"), B(text="🏠", callback_data="menu")])
    return text, KB(inline_keyboard=rows)


async def my_rows(uid):
    return await db.fetch("select * from listings where owner=$1 and status<>'deleted' order by id desc", uid)


async def show_my(c, lang, p=0, lid=None):
    """Shows one of the user's listings in the current message. lid keeps the same listing on screen after a change."""
    uid = c.from_user.id
    rows = await my_rows(uid)
    if not rows:
        kb = KB(inline_keyboard=[[B(text=t(lang, "b_new"), callback_data="new"), B(text="🏠", callback_data="menu")]])
        return await screen(c, "empty", t(lang, "empty"), kb)
    if lid is not None:
        p = next((i for i, x in enumerate(rows) if x["id"] == lid), p)
    p = max(0, min(p, len(rows) - 1))
    text, kb = await listing_view(rows[p], lang, await free_coins(uid), p, len(rows))
    await screen(c, "my", text, kb)


async def refresh_listing_msg(c, lid, lang):
    await show_my(c, lang, lid=lid)


@r.callback_query(F.data == "my")
async def my(c: CallbackQuery, lang):
    await show_my(c, lang)
    await c.answer()


@r.callback_query(F.data.regexp(r"^my:\d+$"))
async def my_page(c: CallbackQuery, lang):
    await show_my(c, lang, int(c.data[3:]))
    await c.answer()


@r.callback_query(F.data.startswith("lr:"))
async def renew(c: CallbackQuery, lang):
    """lr:ID comes from the expiry reminder (left as it is), lr:ID:m from the "My listings" screen (refreshed)."""
    parts = c.data.split(":")
    lid = int(parts[1])
    await db.execute("update listings set updated=now(), warned=false where id=$1 and owner=$2 and status<>'deleted'",
                     lid, c.from_user.id)
    await c.answer(t(lang, "done"), show_alert=True)
    if len(parts) > 2:
        await show_my(c, lang, lid=lid)


async def purge_deleted():
    """Removes deleted listings that never had claims. Listings with claims stay hidden as 'deleted',
    so open claims keep their reserve and the task history and coin journal stay intact."""
    await db.execute("""delete from listings l where l.status='deleted' and not exists
        (select 1 from claims c where c.listing_id=l.id)""")


@r.callback_query(F.data.startswith("ld:"))
async def delete(c: CallbackQuery, lang):
    lid, uid = int(c.data[3:]), c.from_user.id
    ids = [x["id"] for x in await my_rows(uid)]
    await db.execute("update listings set status='deleted' where id=$1 and owner=$2", lid, uid)
    await purge_deleted()
    await refresh_owner(uid)
    await c.answer(t(lang, "done"))
    await show_my(c, lang, ids.index(lid) if lid in ids else 0)


@r.callback_query(F.data.startswith("lp:"))
async def price_cycle(c: CallbackQuery, lang):
    lid, mx = int(c.data[3:]), max(1, await num("price_max"))
    n = await db.fetchval("""update listings set price = case when price >= $3 then 1 else price + 1 end
        where id=$1 and owner=$2 and status<>'deleted' returning price""", lid, c.from_user.id, mx)
    if not n:
        return await c.answer()
    await refresh_owner(c.from_user.id, c.bot)
    await refresh_listing_msg(c, lid, lang)
    await c.answer(t(lang, "price_set", n=n), show_alert=True)


@r.callback_query(F.data.startswith("pm:"))
async def proof_mode(c: CallbackQuery, lang):
    lid = int(c.data[3:])
    n = await db.fetchval(
        "update listings set proof_mode = case when proof_mode=1 then 2 else 1 end where id=$1 and owner=$2 returning proof_mode",
        lid, c.from_user.id)
    if not n:
        return await c.answer()
    await refresh_listing_msg(c, lid, lang)
    await c.answer(t(lang, "pm_set", n=n), show_alert=True)


@r.callback_query(F.data.startswith("lb:"))
async def boost(c: CallbackQuery, lang):
    lid, uid = int(c.data[3:]), c.from_user.id
    price, hours = await num("boost_price"), max(1, await num("boost_hours"))
    if price <= 0:
        return await c.answer(t(lang, "boost_off"), show_alert=True)
    async with db.acquire() as con:
        async with con.transaction():
            await con.execute("select 1 from users where id=$1 for update", uid)
            f = await free_coins(uid, con)
            ok = f >= price and await con.fetchval(
                "select exists(select 1 from listings where id=$1 and owner=$2 and status='active')", lid, uid)
            if ok:
                await add_coins(con, uid, -price, "boost")
                await con.execute("""update listings set boosted_until = greatest(coalesce(boosted_until, now()), now())
                    + make_interval(hours => $2) where id=$1""", lid, hours)
    if not ok:
        return await c.answer(t(lang, "boost_no", n=price, f=f), show_alert=True)
    await refresh_owner(uid, c.bot)
    await refresh_listing_msg(c, lid, lang)
    await c.answer(t(lang, "boost_ok", h=hours, n=price), show_alert=True)


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
    rows = await db.fetch("""select c.status, c.created, c.price, c.reward, coalesce(l.title, '—') title from claims c
        left join listings l on l.id=c.listing_id where c.giver=$1 order by c.created desc limit 200""", uid)

    def block(statuses, label):
        items = [x for x in rows if x["status"] in statuses]
        body = "\n".join(f"• {esc(x['title'][:30])} · {x['created']:%d.%m} · {x['reward'] or x['price']} {COIN}"
                         for x in items[:4])
        if not items:
            body = t(lang, "tk_empty")
        more = "\n" + t(lang, "tk_more", n=len(items) - 4) if len(items) > 4 else ""
        return f"<b>{t(lang, label)} ({len(items)})</b>\n{body}{more}"

    return "\n\n".join([block(("pending", "disputed"), "tk_pending"),
                         block(("confirmed",), "tk_done"),
                         block(("rejected", "expired"), "tk_rejected")])


@r.callback_query(F.data == "tasks")
async def tasks_view(c: CallbackQuery, lang):
    await screen(c, "tasks", t(lang, "tasks_head") + "\n\n" + await tasks_text(c.from_user.id, lang), sub_kb(lang))
    await c.answer()


# --- rules and profile ---
@r.callback_query(F.data == "rules:ok")
async def rules_accept(c: CallbackQuery, lang):
    await db.execute("update users set rules_ok=true where id=$1", c.from_user.id)
    await cat(c, lang)


@r.callback_query(F.data == "rules:view")
async def rules_view(c: CallbackQuery, lang):
    await screen(c, "rules", await rules_caption(lang), sub_kb(lang))
    await c.answer()


@r.callback_query(F.data == "rt")
async def rating_main(c: CallbackQuery, lang):
    await screen(c, "rating", await rating_text(c.from_user.id, lang), rating_kb(lang))
    await c.answer()


@r.callback_query(F.data == "rt:what")
async def rating_what(c: CallbackQuery, lang):
    await screen(c, "rating", t(lang, "rt_what_text"), sub_kb(lang))
    await c.answer()


@r.callback_query(F.data == "rt:top")
async def rating_top(c: CallbackQuery, lang):
    rows = await db.fetch("select username, rating from users where rating>0 and not banned order by rating desc, id limit 10")
    body = "\n".join(f"{i}. {('@' + esc(x['username'])) if x['username'] else '—'} · {x['rating']}"
                     for i, x in enumerate(rows, 1))
    await screen(c, "rating", t(lang, "rt_top_title") + (body if rows else t(lang, "rt_top_empty")), sub_kb(lang))
    await c.answer()


# --- coins ---
async def tx_lines(uid, lang, limit):
    rows = await db.fetch("select delta, kind, created from coin_tx where uid=$1 order by id desc limit $2", uid, limit)
    return "\n".join(f"{'🟢' if x['delta'] > 0 else '🔴'} {x['delta']:+d} · {x['created']:%d.%m} · {tx_label(lang, x['kind'])}"
                     for x in rows) or t(lang, "tx_none")


@r.callback_query(F.data == "coins")
async def coins_view(c: CallbackQuery, lang):
    uid = c.from_user.id
    u = await db.fetchrow("select coins, free_coins(id) f from users where id=$1", uid)
    text = t(lang, "coins_main", c=u["coins"], h=u["coins"] - u["f"], f=u["f"],
             warn=t(lang, "coins_warn") if u["f"] <= 0 else "", tx=await tx_lines(uid, lang, 6))
    kb = KB(inline_keyboard=[[B(text=t(lang, "b_what"), callback_data="coins:how"), B(text=t(lang, "b_cat"), callback_data="cat")],
                             [B(text=t(lang, "b_invite"), callback_data="inv")], [B(text="🏠", callback_data="menu")]])
    await screen(c, "coins", text, kb)
    await c.answer()


@r.callback_query(F.data == "coins:how")
async def coins_how(c: CallbackQuery, lang):
    bp, bh, inv, fee = await num("boost_price"), await num("boost_hours"), await num("invite_bonus"), await num("fee_pct")
    text = t(lang, "coins_how", mx=max(1, await num("price_max")), pen=await setting("penalty_per_strike"),
             boost=t(lang, "how_boost", n=bp, h=bh) if bp > 0 else "",
             invite=t(lang, "how_invite", n=inv, k=max(1, await num("invite_tasks"))) if inv > 0 else "",
             fee=t(lang, "how_fee", n=fee) if fee > 0 else "")
    await screen(c, "coins", text, sub_kb(lang, back="coins"))
    await c.answer()


@r.callback_query(F.data == "inv")
async def invite_view(c: CallbackQuery, lang):
    uid = c.from_user.id
    me = await c.bot.me()
    r_ = await db.fetchrow("select count(*) a, count(*) filter (where invite_paid) b from users where invited_by=$1", uid)
    bonus = await num("invite_bonus")
    text = t(lang, "invite_text", link=f"https://t.me/{me.username}?start=ref_{uid}", a=r_["a"], b=r_["b"],
             bonus=t(lang, "invite_bonus", n=bonus, k=max(1, await num("invite_tasks"))) if bonus > 0 else "")
    await screen(c, "invite", text, sub_kb(lang, back="coins"))
    await c.answer()


# --- notification settings ---
async def notif_view(c, lang):
    off = await db.fetchval("select notif_off from users where id=$1", c.from_user.id) or []
    rows = [[B(text=("🔕 " if k in off else "✅ ") + t(lang, "nt_" + k), callback_data="nt:" + k)] for k in NOTIF_KEYS]
    rows.append([B(text="🏠", callback_data="menu")])
    await screen(c, "notif", t(lang, "notif_head"), KB(inline_keyboard=rows))
    await c.answer()


@r.callback_query(F.data == "nt")
async def notif_open(c: CallbackQuery, lang):
    await notif_view(c, lang)


@r.callback_query(F.data.startswith("nt:"))
async def notif_toggle(c: CallbackQuery, lang):
    k = c.data[3:]
    if k in NOTIF_KEYS:
        await db.execute("""update users set notif_off = case when $2 = any(notif_off) then array_remove(notif_off, $2)
            else array_append(notif_off, $2) end where id=$1""", c.from_user.id, k)
    await notif_view(c, lang)


# --- referral claims ---
async def forward_proof(bot, to_uid, from_uid, mid, fid):
    """Forwards the executor's screenshot, so the author sees who sent it. Falls back to a plain copy."""
    try:
        if mid is None:
            raise ValueError("no message id")
        await bot.forward_message(to_uid, from_uid, mid)
    except Exception:
        await bot.send_photo(to_uid, fid)


async def proof_used(file_uid):
    """True when this exact picture was already sent as proof in any claim (Telegram keeps file_unique_id per file)."""
    return await db.fetchval("""select exists(select 1 from claims where proof_uid=$1)
        or exists(select 1 from claims where proof_before_uid=$1)""", file_uid)


async def claim_create(m: Message, lang, lid, proof, mid, before=None, mid_before=None, proof_uid=None, before_uid=None):
    x = await db.fetchrow("select id, owner, price from listings where id=$1 and status='active'", lid)
    if not x:
        return await m.answer(t(lang, "claim_gone"), reply_markup=home_kb())
    reason = await claim_block_reason(m.from_user.id, x, lang)
    if reason:
        return await m.answer(reason, reply_markup=home_kb())
    try:
        async with db.acquire() as con:
            async with con.transaction():
                # lock the author's row: two claims at the same moment can't both take the last coin
                await con.execute("select 1 from users where id=$1 for update", x["owner"])
                if await free_coins(x["owner"], con) < x["price"]:
                    raise _NoCoins()
                cid = await con.fetchval(
                    """insert into claims(listing_id, giver, receiver, proof, mid, proof_before, mid_before, price,
                                          proof_uid, proof_before_uid)
                       values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) returning id""",
                    lid, m.from_user.id, x["owner"], proof, mid, before, mid_before, x["price"], proof_uid, before_uid)
    except asyncpg.UniqueViolationError:
        return await m.answer(t(lang, "claim_pair"), reply_markup=home_kb())
    except _NoCoins:
        return await m.answer(t(lang, "claim_nocoins"), reply_markup=home_kb())
    await m.answer(t(lang, "claim_sent", p=calc_reward(x["price"], await num("fee_pct")), d=await setting("claim_hours")),
                   reply_markup=home_kb())
    await send_claim_to_author(m.bot, cid)
    await owner_state(x["owner"], m.bot)


@r.callback_query(F.data.startswith("cl:"))
async def claim_start(c: CallbackQuery, lang, state: FSMContext):
    lid = int(c.data[3:])
    x = await db.fetchrow("select id, owner, proof_mode, price from listings where id=$1 and status='active'", lid)
    if not x:
        return await c.answer(t(lang, "claim_gone"), show_alert=True)
    if x["owner"] == c.from_user.id:
        return await c.answer()
    reason = await claim_block_reason(c.from_user.id, x, lang)
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
    puid = m.photo[-1].file_unique_id
    if await proof_used(puid):
        return await m.answer(t(lang, "proof_dup"), reply_markup=cancel_kb(lang))
    await state.update_data(before=m.photo[-1].file_id, before_uid=puid, mid_before=m.message_id)
    await state.set_state(Claim.after)
    await m.answer(t(lang, "claim_after_ask"), reply_markup=cancel_kb(lang))


@r.message(Claim.after, F.photo)
async def claim_after(m: Message, lang, state: FSMContext):
    d = await state.get_data()
    if "lid" not in d:
        await state.clear()
        return await send_menu(m, lang)
    puid = m.photo[-1].file_unique_id
    if puid == d.get("before_uid"):
        return await m.answer(t(lang, "proof_same"), reply_markup=cancel_kb(lang))
    if await proof_used(puid):
        return await m.answer(t(lang, "proof_dup"), reply_markup=cancel_kb(lang))
    await state.clear()
    await claim_create(m, lang, d["lid"], m.photo[-1].file_id, m.message_id, d.get("before"), d.get("mid_before"),
                       puid, d.get("before_uid"))


@r.message(Claim.proof, F.photo)
async def claim_proof(m: Message, lang, state: FSMContext):
    d = await state.get_data()
    if "lid" not in d:
        await state.clear()
        return await send_menu(m, lang)
    puid = m.photo[-1].file_unique_id
    if await proof_used(puid):
        return await m.answer(t(lang, "proof_dup"), reply_markup=cancel_kb(lang))
    await state.clear()
    await claim_create(m, lang, d["lid"], m.photo[-1].file_id, m.message_id, proof_uid=puid)


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
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    lim = await num("disputes_per_10")
    if lim:
        used = await db.fetchval("""select count(*) filter (where disputed_at is not null) from
            (select disputed_at from claims where receiver=$1 and status <> 'pending' order by id desc limit 10) s""",
                                 c.from_user.id)
        if used >= lim:
            return await c.answer(t(lang, "dispute_limit", n=used), show_alert=True)
    # The buttons stay on the claim until the reason is sent: if the author cancels, they can still decide.
    await state.set_state(Reason.text)
    await state.update_data(cid=cid, amid=c.message.message_id)
    await c.message.answer(t(lang, "reason_ask"), reply_markup=cancel_kb(lang))
    await c.answer()


@r.message(Reason.text, F.text)
async def reason_text(m: Message, lang, state: FSMContext):
    reason = m.text.strip()[:300]
    if len(reason) < 3:
        return await m.answer(t(lang, "reason_ask"), reply_markup=cancel_kb(lang))
    d = await state.get_data()
    cid, amid = d.get("cid"), d.get("amid")
    await state.clear()
    row = await db.fetchrow("""update claims set status='disputed', disputed_at=now(), reason=$2
        where id=$1 and status='pending' and receiver=$3 returning giver""", cid, reason, m.from_user.id)
    if amid:  # the claim is decided either way now: remove its buttons
        try:
            await m.bot.edit_message_reply_markup(chat_id=m.chat.id, message_id=amid, reply_markup=None)
        except Exception:
            pass
    if not row:
        return await m.answer(t(lang, "claim_done"), reply_markup=home_kb())
    try:
        await send_screen(m.bot, row["giver"], "dispute", t(await user_lang(row["giver"]), "claim_disputed", r=esc(reason)))
    except Exception as e:
        log.warning("dispute notice to %s failed: %s", row["giver"], e)
    await notify_admins_dispute(m.bot, cid)
    await m.answer(t(lang, "dispute_sent"), reply_markup=home_kb())


@r.message(Reason.text)
async def reason_need_text(m: Message, lang):
    await m.answer(t(lang, "reason_ask"), reply_markup=cancel_kb(lang))


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
    recent = await db.fetchval("""select count(*) from chat_msgs where uid=$1 and not from_admin
        and created > now() - make_interval(mins => $2)""", m.from_user.id, SUP_WINDOW_MIN)
    if recent >= SUP_LIMIT:
        return await m.answer(t(lang, "sup_slow"), reply_markup=support_kb(lang))
    await chat_log(m.from_user.id, False, m.text or m.caption or "[медіа]")
    for a in ADMIN_IDS:
        try:
            await m.forward(a)
            await m.bot.send_message(
                a, f"💬 Нове повідомлення від <code>{m.from_user.id}</code> @{esc(m.from_user.username or '—')}",
                reply_markup=KB(inline_keyboard=[[B(text="💬 Відкрити чат", callback_data=f"ch:{m.from_user.id}"),
                                                  B(text="👤 Картка", callback_data=f"uc:{m.from_user.id}")]]))
        except Exception:
            log.exception("could not forward support message to admin")
    await m.answer(t(lang, "sup_got"), reply_markup=support_kb(lang))


@r.callback_query(F.data == "supdone")
async def support_done(c: CallbackQuery, lang, state: FSMContext):
    await state.clear()
    await db.execute("update chats set status='closed', updated=now() where uid=$1", c.from_user.id)
    for a in ADMIN_IDS:
        try:
            await c.bot.send_message(a, f"✅ Користувач <code>{c.from_user.id}</code> закрив питання.")
        except Exception as e:
            log.warning("admin %s not reachable: %s", a, e)
    await send_screen(c.bot, c.message.chat.id, "support", t(lang, "sup_closed"), home_kb())
    await c.answer()


# ---------- ADMIN ROUTER (ukrainian only) ----------
ar = Router()
ar.message.filter(F.from_user.id.in_(ADMIN_IDS))
ar.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

SET_NAMES = {"ttl_days": "Термін життя анкети (дні)", "report_threshold": "Поріг скарг для приховування",
             "sub_required": "Обов'язкова підписка (1 - так, 0 - ні)", "sub_channel": "Канал (@username або ID)",
             "claim_hours": "Годин на відповідь автора (автозарахування)",
             "price_max": "Максимальна ціна завдання (монет)",
             "fee_pct": "Комісія бота, % від оплати (0 = без комісії)",
             "penalty_per_strike": "Штраф за фейк (монет)",
             "start_coins": "Стартові монети новим користувачам",
             "invite_bonus": "Бонус за запрошеного друга (монет)",
             "invite_tasks": "Скільки різних авторів мають підтвердити завдання друга, щоб дати бонус",
             "boost_price": "Ціна підняття анкети в топ (0 = вимкнено)",
             "boost_hours": "Скільки годин анкета в топі",
             "open_claims_max": "Макс. відкритих заявок у виконавця (0 = без ліміту)",
             "pair_cooldown_days": "Пауза між завданнями тому самому автору, днів (0 = вимк.)",
             "new_days": "Скільки днів акаунт вважається новим",
             "new_daily_claims": "Ліміт заявок на добу для нових акаунтів (0 = вимк.)",
             "disputes_per_10": "Макс. спорів автора на 10 останніх заявок (0 = вимк.)",
             "welcome_ru": "Привітання RU", "welcome_uk": "Привітання UK", "welcome_en": "Привітання EN"}

NOT_CMD = F.func(lambda m: not (m.text or "").startswith("/"))  # commands always reach their handlers
ADMIN_HELP = ("🛠 <b>Адмін-панель</b>\n\n"
              "Картка користувача: /user ID або /user @username\n"
              "Монети всім: /giveall N [причина]\n"
              "Бан: /ban ID, розбан: /unban ID")


def admin_kb():
    rows = [[("📊 Статистика", "adm:stats"), ("👥 Користувачі", "adm:users")],
            [("⚖️ Спори", "adm:disp"), ("💬 Чати", "adm:chats")],
            [("⚠️ Скарги", "adm:rep"), ("📣 Розсилка", "adm:bc")],
            [("⚙️ Налаштування", "adm:set")]]
    return KB(inline_keyboard=[[B(text=a, callback_data=b) for a, b in row] for row in rows])


@ar.message(Command("admin"))
async def admin(m, state: FSMContext):
    await state.clear()
    await m.answer(ADMIN_HELP, reply_markup=admin_kb())


@ar.callback_query(F.data == "adm")
async def admin_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.answer(ADMIN_HELP, reply_markup=admin_kb())
    await c.answer()


@ar.message(Command("ban"))
async def ban(m, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        return await m.answer("Використання: /ban ID")
    await ban_user(int(command.args))
    await m.answer("Забанено, анкети видалено, відкриті заявки скасовано.")


@ar.message(Command("unban"))
async def unban(m, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        return await m.answer("Використання: /unban ID")
    await db.execute("update users set banned=false where id=$1", int(command.args))
    _seen.pop(int(command.args), None)
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
      count(*) filter (where last_seen > now()-interval '7 days') a7,
      coalesce(sum(coins) filter (where not banned), 0) coins,
      count(*) filter (where coins < 0 and not banned) debtors,
      count(*) filter (where locked and not banned) paused
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
    tx = await db.fetchrow("""select coalesce(sum(delta) filter (where kind='earn'), 0) earn7,
      count(*) filter (where kind='earn') tasks7,
      coalesce(-sum(delta) filter (where kind='boost'), 0) boost7,
      coalesce(sum(delta) filter (where kind in ('admin','invite','start')), 0) gift7
      from coin_tx where created > now()-interval '7 days'""")
    held = await db.fetchval("select coalesce(sum(price), 0) from claims where status in ('pending','disputed')")
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
        "coins": u["coins"], "held": held, "debtors": u["debtors"], "paused": u["paused"],
        "earn7": tx["earn7"], "tasks7": tx["tasks7"], "boost7": tx["boost7"], "gift7": tx["gift7"],
    }


def coins_text(s):
    return ("🪙 <b>Монети</b>\n"
            f"В обігу: {s['coins']} · у резерві: {s['held']}\n"
            f"Підтверджених завдань за 7 днів: {s['tasks7']} (+{s['earn7']} {COIN})\n"
            f"Витрачено на підняття: {s['boost7']} · подаровано: {s['gift7']}\n"
            f"Анкети на паузі у {s['paused']} користувачів · у мінусі: {s['debtors']}")


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
        f"Переглядів: за добу {s['v1']}, за тиждень {s['v7']}, за 60 днів {s['vall']}\n\n"
        "⚠️ <b>Скарги</b>\n"
        f"За тиждень: {s['r7']}\n\n" + coins_text(s)
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
        ("Монет в обігу", s["coins"], f"резерв: {s['held']}"),
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

    details = [
        ("Нових за добу", f"+{s['n1']}"),
        ("Нових за місяць", f"+{s['n30']}"),
        ("Переглядів за 7 днів", s["v7"]),
        ("Завдань за 7 днів", s["tasks7"]),
        ("Анкети на паузі", s["paused"]),
        ("Прихованих анкет", s["hidden"]),
        ("Скарг за тиждень", s["r7"]),
        ("Забанено", s["banned"]),
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
        await c.message.answer_photo(BufferedInputFile(png, filename="stats.png"), caption=coins_text(s), reply_markup=admin_kb())
    except Exception:
        log.exception("stats image failed, sending text instead")
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
                                         [B(text="✉️ Написати автору", callback_data=f"chw:{x['owner']}"),
                                          B(text="👤 Картка автора", callback_data=f"uc:{x['owner']}")]]))


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
        await db.execute("update listings set status='deleted' where id=$1", lid)
        await purge_deleted()
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
    rows.append([B(text="🚪 Адмін-панель", callback_data="adm")])
    await c.message.answer("⚙️ <b>Налаштування</b>\n\n" + "\n".join(lines), reply_markup=KB(inline_keyboard=rows))
    await c.answer()


@ar.callback_query(F.data == "adm:subtog")
async def sub_toggle(c: CallbackQuery):
    new = "0" if await setting("sub_required") == "1" else "1"
    await set_setting("sub_required", new)
    await settings_menu(c)


@ar.callback_query(F.data.startswith("as:"))
async def setting_ask(c: CallbackQuery, state: FSMContext):
    k = c.data[3:]
    if k not in SET_NAMES:
        return await c.answer()
    await state.set_state(Adm.val)
    await state.update_data(key=k)
    await c.message.answer(f"Надішліть нове значення для «{SET_NAMES[k]}» (або «-», щоб очистити):",
                           reply_markup=cancel_kb("uk"))
    await c.answer()


@ar.message(Adm.val, F.text, NOT_CMD)
async def setting_save(m, state: FSMContext):
    k = (await state.get_data()).get("key")
    if k not in SET_NAMES:
        await state.clear()
        return await m.answer("Сесію налаштування втрачено, відкрийте її знову.", reply_markup=admin_kb())
    v = "" if m.text.strip() == "-" else m.text.strip()
    if k in NUM_KEYS and not v.isdigit():
        return await m.answer("Потрібне ціле число (0 або більше).")
    if k == "sub_required" and v not in ("0", "1"):
        return await m.answer("Надішліть 0 або 1.")
    if k == "fee_pct" and int(v) > 90:
        return await m.answer("Комісія не може бути більшою за 90%.")
    if k in ("price_max", "claim_hours", "boost_hours") and int(v) < 1:
        return await m.answer("Потрібно щонайменше 1.")
    await set_setting(k, v)
    await state.clear()
    await m.answer("Збережено ✅", reply_markup=admin_kb())


# --- broadcast (skips users who turned off news) ---
@ar.callback_query(F.data == "adm:bc")
async def bc_ask(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.bc)
    await c.message.answer("Надішліть текст розсилки (HTML дозволено). Її отримають усі, хто не вимкнув «Новини й розсилки».",
                           reply_markup=cancel_kb("uk"))
    await c.answer()


@ar.message(Adm.bc, F.text, NOT_CMD)
async def bc_preview(m, state: FSMContext):
    await state.update_data(text=m.html_text)
    await m.answer("Попередній перегляд:\n\n" + m.html_text,
                   reply_markup=KB(inline_keyboard=[[B(text="📣 Надіслати всім", callback_data="bc:go")],
                                                    [B(text="❌ Скасувати", callback_data="adm")]]))


BC_RECIPIENTS = "not banned and not ('news' = any(notif_off))"


async def send_bc(bot, bid, uid, text):
    """One broadcast message. Waits out Telegram's flood limit (RetryAfter) instead of skipping the user."""
    for _ in range(3):
        try:
            await bot.send_message(uid, text)
            return True
        except TelegramRetryAfter as e:
            await db.execute("update broadcasts set heartbeat=now() where id=$1 and runner=$2", bid, INSTANCE)
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            return False
        except Exception as e:
            log.warning("broadcast to %s failed: %s", uid, e)
            return False
    return False


async def broadcast(bot, bid):
    """Sends a stored broadcast in user-id order and saves progress every 20 users.
    After a Render restart the new instance picks it up from the saved point (see resume_broadcasts)."""
    b = await db.fetchrow("select * from broadcasts where id=$1 and status='running' and runner=$2", bid, INSTANCE)
    if not b:
        return
    last, ok, sent = b["last_uid"], b["ok"], 0
    while True:
        ids = [x["id"] for x in await db.fetch(
            f"select id from users where id > $1 and {BC_RECIPIENTS} order by id limit 200", last)]
        if not ids:
            break
        for uid in ids:
            if await send_bc(bot, bid, uid, b["text"]):
                ok += 1
            last, sent = uid, sent + 1
            if sent % 20 == 0 and not await db.fetchval(
                    """update broadcasts set last_uid=$2, ok=$3, heartbeat=now() where id=$1 and runner=$4
                       and status='running' returning id""", bid, last, ok, INSTANCE):
                return  # another instance took over or an admin stopped it
            await asyncio.sleep(0.05)
    await db.execute("update broadcasts set status='done', finished=now(), last_uid=$2, ok=$3 where id=$1",
                     bid, last, ok)
    try:
        await bot.send_message(b["admin_id"], f"📣 Розсилку #{bid} завершено: {ok} з {b['total']}", reply_markup=admin_kb())
    except Exception as e:
        log.warning("broadcast report failed: %s", e)


async def resume_broadcasts(bot):
    """Runs once after start. Waits until the old instance (during a deploy) has surely stopped, then takes over
    broadcasts whose heartbeat went quiet."""
    await asyncio.sleep(150)
    try:
        for x in await db.fetch("""update broadcasts set runner=$1, heartbeat=now() where status='running'
                and (heartbeat is null or heartbeat < now() - interval '120 seconds') returning id""", INSTANCE):
            log.info("resuming broadcast %s", x["id"])
            _bg(broadcast(bot, x["id"]))
    except Exception:
        log.exception("could not resume broadcasts")


@ar.callback_query(F.data == "bc:go")
async def bc_go(c: CallbackQuery, state: FSMContext):
    text = (await state.get_data()).get("text")
    await state.clear()
    if not text:
        return await c.answer("Немає тексту")
    if await db.fetchval("select exists(select 1 from broadcasts where status='running')"):
        return await c.answer("Попередня розсилка ще триває. Дочекайтеся звіту.", show_alert=True)
    total = await db.fetchval(f"select count(*) from users where {BC_RECIPIENTS}")
    bid = await db.fetchval("""insert into broadcasts(admin_id, text, total, runner, heartbeat)
        values($1,$2,$3,$4,now()) returning id""", c.from_user.id, text, total, INSTANCE)
    await c.answer("Розсилаю...")
    _bg(broadcast(c.bot, bid))


# --- moderation: disputes ---
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
    ok = await (apply_confirmed(c.bot, int(cid), forced=True) if act == "ok" else reject_claim(c.bot, int(cid)))
    await c.answer("Готово" if ok else "Вже розглянуто")
    await next_dispute(c.message)


# --- chats: support replies and personal messages to any user ---
async def chat_log(uid, from_admin, text):
    await db.execute("insert into chat_msgs(uid, from_admin, text) values($1,$2,$3)", uid, from_admin, text[:4000])
    await db.execute("""insert into chats(uid, status, updated) values($1,'open',now())
        on conflict(uid) do update set status='open', updated=now()""", uid)


def chat_kb(uid):
    return KB(inline_keyboard=[
        [B(text="✍️ Написати", callback_data=f"chw:{uid}"), B(text="👤 Картка", callback_data=f"uc:{uid}")],
        [B(text="✅ Закрити питання", callback_data=f"chx:{uid}"), B(text="🚪 Вийти", callback_data="adm")]])


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
    await c.message.answer(f"✍️ Пишіть повідомлення для <code>{uid}</code>: текст, фото, відео чи файл. "
                           f"Кожне піде користувачу з кнопкою «Відповісти». «🚪 Вийти» — завершити.",
                           reply_markup=chat_kb(uid))
    await c.answer()


@ar.message(Adm.chat, NOT_CMD)
async def chat_send(m: Message, state: FSMContext):
    uid = (await state.get_data()).get("uid")
    if not uid:
        await state.clear()
        return await m.answer("Чат не обрано.", reply_markup=admin_kb())
    lang = await user_lang(uid)
    support_open = await db.fetchval("select status='open' from chats where uid=$1", uid)
    reply_kb = KB(inline_keyboard=[[B(text=t(lang, "b_reply"), callback_data="sup")]])
    await chat_log(uid, True, m.text or m.caption or "[медіа]")
    try:
        if m.text:
            await m.bot.send_message(uid, t(lang, "sup_reply" if support_open else "adm_msg", text=m.html_text),
                                     reply_markup=reply_kb)
        else:
            await m.bot.send_message(uid, t(lang, "adm_msg_media"))
            await m.bot.copy_message(uid, m.chat.id, m.message_id, reply_markup=reply_kb)
        note = "Надіслано ✅"
    except Exception as e:
        log.info("admin message to %s failed: %s", uid, e)
        note = "Не вдалося доставити: можливо, користувач заблокував бота. Текст збережено в історії."
    await m.answer(note, reply_markup=chat_kb(uid))


@ar.callback_query(F.data.regexp(r"^chx:\d+$"))
async def chat_close(c: CallbackQuery):
    uid = int(c.data[4:])
    await db.execute("update chats set status='closed', updated=now() where uid=$1", uid)
    await notify(c.bot, uid, "sup_admin_closed")
    await c.answer("Питання закрито")
    await chat_view(c.message, uid)


# --- users: search, card, coins, personal messages ---
async def adm_notify_on(aid):
    """Admin's choice: tell users about coin changes made by this admin (default: yes)."""
    return await setting(f"admn_{aid}") != "0"


async def user_card(uid, aid):
    u = await db.fetchrow("select *, free_coins(id) f from users where id=$1", uid)
    if not u:
        return None, None
    l = await db.fetchrow("""select count(*) n, count(*) filter (where status='active') a from listings
        where owner=$1 and status<>'deleted'""", uid)
    open_n = await db.fetchval("select count(*) from claims where giver=$1 and status in ('pending','disputed')", uid)
    invited = await db.fetchval("select count(*) from users where invited_by=$1", uid)
    s = await owner_stats(uid)
    pct = f"{s['pct']}% ({s['total']})" if s["total"] else "—"
    off = ", ".join(u["notif_off"]) if u["notif_off"] else "—"
    seen = f"{u['last_seen']:%d.%m %H:%M}" if u["last_seen"] else "—"
    tx = await db.fetch("select delta, kind, note, created from coin_tx where uid=$1 order by id desc limit 5", uid)
    tx_body = "\n".join(f"{x['delta']:+d} · {x['created']:%d.%m} · {tx_label('uk', x['kind'])}"
                        f"{(' · ' + esc(x['note'][:40])) if x['note'] else ''}" for x in tx) or "—"
    text = (f"👤 <code>{uid}</code> @{esc(u['username'] or '—')} · мова: {u['lang'] or '—'}\n"
            f"🪙 Монет: <b>{u['coins']}</b> · у резерві: {u['coins'] - u['f']} · вільно: <b>{u['f']}</b>"
            f"{' · ⏸ анкети на паузі' if u['locked'] else ''}\n"
            f"🏆 Рейтинг: {u['rating']} · ⚠️ Фейків: {u['fakes']} · 👍 Погоджує: {pct}\n"
            f"📋 Анкет: {l['n']} (активних {l['a']}) · відкритих заявок: {open_n}\n"
            f"🤝 Запросив: {invited} · запрошений: {('<code>' + str(u['invited_by']) + '</code>') if u['invited_by'] else '—'}\n"
            f"🔕 Вимкнені сповіщення: {off}\n"
            f"📅 З нами з {u['created']:%d.%m.%Y} · був: {seen}\n"
            f"Правила: {'так' if u['rules_ok'] else 'ні'} · Бан: {'так' if u['banned'] else 'ні'}\n\n"
            f"<b>Останні операції</b>\n{tx_body}")
    on = await adm_notify_on(aid)
    kb = KB(inline_keyboard=[
        [B(text=f"{COIN} +1", callback_data=f"ua:{uid}:1"), B(text=f"{COIN} +5", callback_data=f"ua:{uid}:5"),
         B(text=f"{COIN} +10", callback_data=f"ua:{uid}:10")],
        [B(text=f"{COIN} −1", callback_data=f"ua:{uid}:-1"), B(text=f"{COIN} −5", callback_data=f"ua:{uid}:-5"),
         B(text=f"{COIN} −10", callback_data=f"ua:{uid}:-10")],
        [B(text="✏️ Своя сума і причина", callback_data=f"uq:{uid}")],
        [B(text=f"{'🔔' if on else '🔕'} Повідомляти користувача про зміну монет: {'так' if on else 'ні'}",
           callback_data=f"un:{uid}")],
        [B(text="✉️ Написати", callback_data=f"chw:{uid}"), B(text="💬 Історія чату", callback_data=f"ch:{uid}")],
        [B(text="✅ Розбанити" if u["banned"] else "🚫 Забанити", callback_data=f"ub:{uid}")],
        [B(text="🔄 Оновити", callback_data=f"uc:{uid}"), B(text="🚪 Адмін-панель", callback_data="adm")]])
    return text, kb


async def show_card(c: CallbackQuery, uid, note=None):
    text, kb = await user_card(uid, c.from_user.id)
    if text is None:
        return await c.answer("Користувача не знайдено", show_alert=True)
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await c.message.answer(text, reply_markup=kb)
    await c.answer(note or "")


async def find_users(q):
    q = q.strip()
    if q.lstrip("-").isdigit():
        return await db.fetch("select id, username, coins from users where id=$1", int(q))
    q = q.lstrip("@")
    rows = await db.fetch("select id, username, coins from users where lower(username)=lower($1)", q)
    if rows:
        return rows
    return await db.fetch("select id, username, coins from users where username ilike $1 order by last_seen desc nulls last limit 10",
                          f"%{q}%")


def users_kb(rows):
    kb = [[B(text=f"{x['id']} @{x['username'] or '—'} · {x['coins']} {COIN}", callback_data=f"uc:{x['id']}")] for x in rows]
    kb.append([B(text="🚪 Адмін-панель", callback_data="adm")])
    return KB(inline_keyboard=kb)


@ar.callback_query(F.data == "adm:users")
async def users_menu(c: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.find)
    rows = await db.fetch("select id, username, coins from users where not banned order by last_seen desc nulls last limit 10")
    await c.message.answer("👥 <b>Користувачі</b>\n\nНадішліть ID, @username (або частину імені) чи перешліть повідомлення "
                           "користувача, щоб відкрити картку. Нижче — останні активні:", reply_markup=users_kb(rows))
    await c.answer()


@ar.message(Adm.find, NOT_CMD)
async def users_find(m: Message, state: FSMContext):
    origin = getattr(m, "forward_origin", None)
    sender = getattr(origin, "sender_user", None)
    if sender:
        rows = await db.fetch("select id, username, coins from users where id=$1", sender.id)
    elif m.text:
        rows = await find_users(m.text)
    else:
        return await m.answer("Надішліть ID, @username або перешліть повідомлення.")
    if not rows:
        return await m.answer("Нікого не знайдено. Спробуйте ще раз або натисніть «🚪 Адмін-панель».", reply_markup=users_kb([]))
    if len(rows) == 1:
        await state.clear()
        text, kb = await user_card(rows[0]["id"], m.from_user.id)
        return await m.answer(text, reply_markup=kb)
    await m.answer("Знайдено кілька, оберіть:", reply_markup=users_kb(rows))


@ar.message(Command("user"))
async def user_info(m: Message, command: CommandObject):
    rows = await find_users(command.args or "") if (command.args or "").strip() else []
    if not rows:
        return await m.answer("Використання: /user ID або /user @username. Нікого не знайдено.")
    if len(rows) > 1:
        return await m.answer("Знайдено кілька, оберіть:", reply_markup=users_kb(rows))
    text, kb = await user_card(rows[0]["id"], m.from_user.id)
    await m.answer(text, reply_markup=kb)


@ar.callback_query(F.data.regexp(r"^uc:\d+$"))
async def user_card_cb(c: CallbackQuery, state: FSMContext):
    if await state.get_state() == Adm.find.state:
        await state.clear()
    await show_card(c, int(c.data[3:]))


async def admin_adjust(bot, aid, uid, delta, note=None):
    """Adds or removes coins on behalf of an admin, journals it and (if the admin chose so) tells the user."""
    async with db.acquire() as con:
        async with con.transaction():
            if not await con.fetchval("select exists(select 1 from users where id=$1)", uid):
                return False
            await add_coins(con, uid, delta, "admin", admin_id=aid, note=note)
    if await adm_notify_on(aid):
        lang = await user_lang(uid)
        await notify(bot, uid, "n_admin_add" if delta > 0 else "n_admin_sub", n=abs(delta), f=await free_coins(uid),
                     note=t(lang, "n_note", r=esc(note)) if note else "")
    await refresh_owner(uid, bot)
    return True


@ar.callback_query(F.data.regexp(r"^ua:\d+:-?\d+$"))
async def user_adjust(c: CallbackQuery):
    _, uid, d = c.data.split(":")
    ok = await admin_adjust(c.bot, c.from_user.id, int(uid), int(d))
    await show_card(c, int(uid), f"{int(d):+d} {COIN}" if ok else "Користувача не знайдено")


@ar.callback_query(F.data.regexp(r"^un:\d+$"))
async def user_notify_toggle(c: CallbackQuery):
    on = await adm_notify_on(c.from_user.id)
    await set_setting(f"admn_{c.from_user.id}", "0" if on else "1")
    await show_card(c, int(c.data[3:]), "Сповіщення вимкнено" if on else "Сповіщення увімкнено")


@ar.callback_query(F.data.regexp(r"^uq:\d+$"))
async def user_custom_ask(c: CallbackQuery, state: FSMContext):
    uid = int(c.data[3:])
    await state.set_state(Adm.coins)
    await state.update_data(uid=uid)
    await c.message.answer(f"✏️ Надішліть суму для <code>{uid}</code> і, за бажанням, причину. Приклади:\n"
                           f"<code>+20 бонус за активність</code>\n<code>-3 фейкові скріни</code>\n\n"
                           f"Причину користувач побачить, якщо сповіщення увімкнено.", reply_markup=cancel_kb("uk"))
    await c.answer()


@ar.message(Adm.coins, F.text, NOT_CMD)
async def user_custom_save(m: Message, state: FSMContext):
    uid = (await state.get_data()).get("uid")
    mt = re.fullmatch(r"\s*([+-]?\d{1,6})(?:\s+(.+))?", m.text, re.S)
    if not uid or not mt or int(mt.group(1)) == 0:
        return await m.answer("Формат: <code>+20 причина</code> або <code>-3</code>. Сума не може бути 0.",
                              reply_markup=cancel_kb("uk"))
    await state.clear()
    d, note = int(mt.group(1)), (mt.group(2) or "").strip()[:200] or None
    ok = await admin_adjust(m.bot, m.from_user.id, uid, d, note)
    text, kb = await user_card(uid, m.from_user.id)
    await m.answer((f"Готово: {d:+d} {COIN} ✅\n\n" + text) if ok else "Користувача не знайдено.", reply_markup=kb)


@ar.callback_query(F.data.regexp(r"^ub:\d+$"))
async def user_ban_ask(c: CallbackQuery):
    uid = int(c.data[3:])
    if await db.fetchval("select banned from users where id=$1", uid):
        await db.execute("update users set banned=false where id=$1", uid)
        _seen.pop(uid, None)
        return await show_card(c, uid, "Розбанено")
    await c.message.answer(f"Забанити <code>{uid}</code>? Анкети буде видалено, відкриті заявки скасовано.",
                           reply_markup=KB(inline_keyboard=[[B(text="🚫 Так, забанити", callback_data=f"ubc:{uid}"),
                                                             B(text="Ні", callback_data=f"uc:{uid}")]]))
    await c.answer()


@ar.callback_query(F.data.regexp(r"^ubc:\d+$"))
async def user_ban_do(c: CallbackQuery):
    uid = int(c.data[4:])
    await ban_user(uid)
    await show_card(c, uid, "Забанено")


@ar.message(Command("giveall"))
async def give_all(m: Message, command: CommandObject):
    mt = re.fullmatch(r"\s*([+-]?\d{1,4})(?:\s+(.+))?", command.args or "", re.S)
    if not mt or int(mt.group(1)) == 0:
        return await m.answer("Використання: /giveall N [причина], наприклад <code>/giveall 2 старт монет</code>")
    d, note = int(mt.group(1)), (mt.group(2) or "").strip()[:200] or None
    await m.answer(f"Нараховую {d:+d} {COIN} усім незабаненим…")
    _bg(give_all_task(m.bot, m.from_user.id, d, note))


async def give_all_task(bot, aid, d, note):
    async with db.acquire() as con:
        async with con.transaction():
            ids = [x["id"] for x in await con.fetch("update users set coins=coins+$1 where not banned returning id", d)]
            await con.execute("""insert into coin_tx(uid, delta, kind, admin_id, note)
                select id, $1, 'admin', $2, $3 from users where id = any($4::bigint[])""", d, aid, note, ids)
    tell = await adm_notify_on(aid)
    for uid in ids:
        if tell:
            lang = await user_lang(uid)
            await notify(bot, uid, "n_admin_add" if d > 0 else "n_admin_sub", n=abs(d), f=await free_coins(uid),
                         note=t(lang, "n_note", r=esc(note)) if note else "")
            await asyncio.sleep(0.05)
    for x in await db.fetch("select id from users where not banned and (locked or $1 < 0)", d):
        await refresh_owner(x["id"], bot)
    await bot.send_message(aid, f"✅ Нараховано {d:+d} {COIN} для {len(ids)} користувачів"
                                f"{' (з повідомленням)' if tell else ' (без повідомлення)'}.", reply_markup=admin_kb())


# ---------- BACKGROUND JOBS ----------
_tasks = set()


def _bg(coro):
    """Runs a long job (broadcast, mass coins) without blocking the webhook answer."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def send_digests(bot):
    """Once a day (09–21 Kyiv time) tells active users how many new listings appeared. Users can turn it off."""
    if not 6 <= datetime.now(timezone.utc).hour <= 18:
        return
    rows = await db.fetch("""select u.id, u.lang, (select count(*) from listings l join users o on o.id=l.owner
            where l.created > coalesce(u.digest_at, now() - interval '1 day') and l.owner <> u.id
              and l.status='active' and not o.banned and free_coins(l.owner) >= l.price) n
        from users u where not u.banned and u.lang is not null and not ('digest' = any(u.notif_off))
          and coalesce(u.digest_at, '-infinity'::timestamptz) < now() - interval '23 hours'
          and u.last_seen > now() - interval '14 days' and u.created < now() - interval '1 day'
        limit 300""")
    for x in rows:
        await db.execute("update users set digest_at=now() where id=$1", x["id"])
        if x["n"]:
            lang = x["lang"] if x["lang"] in LANGS else "ru"
            await notify(bot, x["id"], "n_digest", cat="digest", n=x["n"],
                         kb=KB(inline_keyboard=[[B(text=t(lang, "b_cat"), callback_data="cat")]]))
            await asyncio.sleep(0.05)


_db_health = {"ok": True, "at": None}  # filled by the hourly loop; /health reads it without touching Neon


async def check_db():
    try:
        await db.fetchval("select 1")
        _db_health.update(ok=True, at=datetime.now(timezone.utc))
    except Exception:
        _db_health.update(ok=False, at=datetime.now(timezone.utc))
        log.exception("database check failed")


async def cleanup_loop(bot: Bot):
    while True:
        await check_db()
        prune_caches()
        try:
            ttl = await num("ttl_days")
            rows = await db.fetch(
                """select l.id, l.owner, l.title, u.lang from listings l join users u on u.id=l.owner
                   where not l.warned and l.status<>'deleted' and l.updated < now() - make_interval(days => $1)""", max(ttl - 1, 0))
            for x in rows:
                lang = x["lang"] if x["lang"] in LANGS else "ru"
                await notify(bot, x["owner"], "expiring", cat="listing", img="expire", t=esc(x["title"]),
                             kb=KB(inline_keyboard=[[B(text=t(lang, "renew"), callback_data=f"lr:{x['id']}")]]))
                await db.execute("update listings set warned=true where id=$1", x["id"])
            await db.execute("update listings set status='deleted' where status<>'deleted' and updated < now() - make_interval(days => $1)", ttl)
            await purge_deleted()
        except Exception:
            log.exception("cleanup failed")
        await claims_expiry(bot)
        try:
            await send_digests(bot)
        except Exception:
            log.exception("digest failed")
        try:  # Neon free has a storage limit: keep only recent history
            await db.execute("delete from events where created < now() - interval '60 days'")
            await db.execute("delete from fsm where updated < now() - interval '7 days'")
        except Exception:
            log.exception("housekeeping failed")
        await asyncio.sleep(3600)


# ---------- STARTUP ----------
async def on_startup(bot: Bot, dispatcher: Dispatcher):
    global db
    db = await asyncpg.create_pool(DSN, min_size=1, max_size=5, statement_cache_size=0)
    await migrate()
    await _load_settings()
    # Keep updates that came while Render was asleep or restarting: they are processed after start.
    await bot.set_webhook(f"{BASE_URL}/webhook", secret_token=SECRET, drop_pending_updates=False,
                          allowed_updates=dispatcher.resolve_used_update_types())
    _bg(cleanup_loop(bot))
    _bg(resume_broadcasts(bot))
    log.info("bot started (instance %s)", INSTANCE)


async def on_shutdown(bot: Bot):
    for task in list(_tasks):
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    if db:
        await db.close()
    log.info("bot stopped")


async def health(_):
    # UptimeRobot pings this every few minutes. It does not query Neon (so Neon can sleep and save compute hours);
    # it reports the result of the hourly database check instead.
    if not _db_health["ok"]:
        return web.Response(status=503, text=f"database error at {_db_health['at']:%Y-%m-%d %H:%M} UTC")
    return web.Response(text="ok")


def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=storage)
    dp.message.outer_middleware(Mw())
    dp.callback_query.outer_middleware(Mw())
    dp.include_router(ar)
    dp.include_router(r)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    app = web.Application()
    app.router.add_get("/health", health)
    SimpleRequestHandler(dp, bot, secret_token=SECRET).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
