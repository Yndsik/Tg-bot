import asyncio
import random
import sqlite3
import time
import aiohttp

TOKEN = "8932170200:AAHpxbAuLChcEkQqaIofxOBUfyN8eVyEvAM"
API_URL = f"https://api.telegram.org/bot{TOKEN}/"

# --- ЗАЩИТА ОТ ФЛУДА И ПЕРЕГРУЗКИ ---
user_cooldowns = {}
COOLDOWN_TIME = 0.5  # Уменьшили задержку для быстрого отклика

# --- 1. БАЗА ДАННЫХ ---
conn = sqlite3.connect("casino.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    first_name TEXT,
    balance INTEGER DEFAULT 1000,
    last_bonus INTEGER DEFAULT 0,
    last_ad INTEGER DEFAULT 0,
    job TEXT DEFAULT 'Безработный',
    last_work INTEGER DEFAULT 0,
    spouse_id INTEGER DEFAULT 0,
    last_rob INTEGER DEFAULT 0
)
""")
conn.commit()

for col, col_type in [
    ("last_ad", "INTEGER DEFAULT 0"),
    ("job", "TEXT DEFAULT 'Безработный'"),
    ("last_work", "INTEGER DEFAULT 0"),
    ("spouse_id", "INTEGER DEFAULT 0"),
    ("last_rob", "INTEGER DEFAULT 0"),
]:
    try:
        cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def get_user(user_id, first_name="Игрок"):
    cursor.execute(
        "SELECT user_id, first_name, balance, job, spouse_id FROM users WHERE user_id = ?",
        (user_id,),
    )
    user = cursor.fetchone()
    if not user:
        cursor.execute(
            "INSERT INTO users (user_id, first_name, balance) VALUES (?, ?, 1000)",
            (user_id, first_name),
        )
        conn.commit()
        return (user_id, first_name, 1000, "Безработный", 0)
    return user


def get_user_name(user_id):
    cursor.execute("SELECT first_name FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    return res[0] if res else "Неизвестный"


def update_balance(user_id, amount):
    cursor.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id = ?",
        (amount, user_id),
    )
    conn.commit()


JOBS = {
    "🏃 Курьер": {"pay": 400, "req": "Доставка еды и посылок"},
    "🚗 Таксист": {"pay": 800, "req": "Перевозка пассажиров"},
    "💻 Программист": {"pay": 2000, "req": "Написание кода и ботов"},
}

pending_proposals = {}
pending_duels = {}
games_21 = {}

# Переменная для хранения aiohttp сессии
http_session: aiohttp.ClientSession = None


# --- 2. БЫСТРЫЕ АСИНХРОННЫЕ СЕТЕВЫЕ ЗАПРОСЫ ---
async def api_request(method: str, params: dict = None):
    url = API_URL + method
    try:
        async with http_session.post(url, json=params or {}, timeout=10) as response:
            return await response.json()
    except Exception as e:
        # Тихий отлов сетевых ошибок без падающих трейсов
        return {"ok": False, "error": str(e)}


async def send_message(chat_id: int, text: str, reply_markup: dict = None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        params["reply_markup"] = reply_markup
    await api_request("sendMessage", params)


async def edit_message_text(
    chat_id: int, message_id: int, text: str, reply_markup: dict = None
):
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    else:
        params["reply_markup"] = {"inline_keyboard": []}
    await api_request("editMessageText", params)


async def answer_callback_query(callback_id: str, text: str):
    await api_request(
        "answerCallbackQuery",
        {"callback_query_id": callback_id, "text": text, "show_alert": True},
    )


# --- 3. КЛАВИАТУРЫ ---
main_keyboard = {
    "keyboard": [
        [{"text": "🎰 Казино"}, {"text": "💼 Работа"}],
        [{"text": "👤 Профиль"}, {"text": "🎁 Ежедневный бонус"}],
        [{"text": "💍 Свадьба"}, {"text": "📺 Реклама (+300$)"}],
        [{"text": "ℹ️ Помощь / Команды"}],
    ],
    "resize_keyboard": True,
}

help_keyboard = {
    "inline_keyboard": [
        [{"text": "💼 Работа и заработок", "callback_data": "help_jobs"}],
        [{"text": "⚔️ Дуэли, Грабеж и Переводы", "callback_data": "help_rp"}],
        [{"text": "🎰 Казино и Азарт", "callback_data": "help_casino"}],
    ]
}

casino_menu_keyboard = {
    "inline_keyboard": [
        [{"text": "🎰 Рулетка", "callback_data": "menu_roulette"}],
        [{"text": "🃏 Игра 21 (Блэкджек)", "callback_data": "menu_21"}],
    ]
}

roulette_keyboard = {
    "inline_keyboard": [
        [
            {"text": "🔴 Красное (x2)", "callback_data": "roulette_red"},
            {"text": "⚫️ Чёрное (x2)", "callback_data": "roulette_black"},
        ],
        [{"text": "🟢 Зеро (x14)", "callback_data": "roulette_zero"}],
    ]
}

game21_keyboard = {
    "inline_keyboard": [
        [{"text": "➕ Взять карту", "callback_data": "21_hit"}],
        [{"text": "✋ Хватит", "callback_data": "21_stand"}],
    ]
}


def get_card():
    return random.choice([2, 3, 4, 6, 7, 8, 9, 10, 11])


def calculate_score(hand):
    score = sum(hand)
    if score > 21 and 11 in hand:
        score -= 10
    return score


# --- 4. ОБРАБОТКА КОМАНД ---
async def handle_update(update: dict):
    user_id = None
    if "message" in update:
        user_id = update["message"]["from"]["id"]
    elif "callback_query" in update:
        user_id = update["callback_query"]["from"]["id"]

    if user_id:
        now = time.time()
        last_time = user_cooldowns.get(user_id, 0)
        if now - last_time < COOLDOWN_TIME:
            if "callback_query" in update:
                await answer_callback_query(
                    update["callback_query"]["id"], "⚠️ Подождите секунду!"
                )
            return
        user_cooldowns[user_id] = now

    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        first_name = msg["from"].get("first_name", "Игрок")
        text = msg.get("text", "").strip()
        text_lower = text.lower()

        get_user(user_id, first_name)

        if text.startswith("/start"):
            await send_message(
                chat_id,
                f"🏰 Добро пожаловать в RP Мир, {first_name}!\n\n"
                f"Здесь вы можете работать, играть в казино, вступать в брак, передавать деньги, грабить и устраивать дуэли!\n"
                f"Вам начислен стартовый капитал: 1000$.\n\n"
                f"Нажмите «ℹ️ Помощь / Команды», чтобы узнать все возможности.",
                reply_markup=main_keyboard,
            )

        elif text in ["ℹ️ Помощь / Команды", "/help", "помощь"]:
            await send_message(
                chat_id,
                "📖 *Справочное бюро*\n\nВыберите категорию, чтобы узнать подробнее о командах и механиках:",
                reply_markup=help_keyboard,
            )

        elif text == "👤 Профиль":
            cursor.execute(
                "SELECT balance, job, spouse_id FROM users WHERE user_id = ?",
                (user_id,),
            )
            balance, job, spouse_id = cursor.fetchone()

            spouse_text = (
                f"💍 В браке с: {get_user_name(spouse_id)}"
                if spouse_id
                else "💍 Статус: Холост / Не замужем"
            )

            await send_message(
                chat_id,
                f"👤 *Ваш RP Профиль:*\n\n"
                f"📝 Имя: {first_name}\n"
                f"💰 Баланс: {balance}$\n"
                f"💼 Работа: {job}\n"
                f"{spouse_text}",
            )

        elif text == "💼 Работа":
            cursor.execute(
                "SELECT job, last_work FROM users WHERE user_id = ?", (user_id,)
            )
            job, last_work = cursor.fetchone()

            jobs_btn = [
                [
                    {
                        "text": f"Устроиться: {name} ({data['pay']}$/час)",
                        "callback_data": f"job_set_{name}",
                    }
                ]
                for name, data in JOBS.items()
            ]

            if job != "Безработный":
                jobs_btn.append(
                    [{"text": "🛠 Поработать (Получить ЗП)", "callback_data": "job_work"}]
                )

            await send_message(
                chat_id,
                f"💼 *Центр Занятости*\n\n"
                f"Ваша текущая работа: {job}\n"
                f"Выберите вакансию или отработайте смену:",
                reply_markup={"inline_keyboard": jobs_btn},
            )

        elif text == "💍 Свадьба":
            cursor.execute(
                "SELECT spouse_id FROM users WHERE user_id = ?", (user_id,)
            )
            spouse_id = cursor.fetchone()[0]

            if spouse_id:
                await send_message(
                    chat_id,
                    f"💍 Вы уже состоите в браке с {get_user_name(spouse_id)}!",
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": "💔 Развестись", "callback_data": "divorce"}]
                        ]
                    },
                )
            else:
                await send_message(
                    chat_id,
                    "💍 *Как вступить в брак?*\n\n"
                    "1. Ответьте (Reply) на сообщение нужного человека в чате.\n"
                    "2. Напишите в ответе: `Брак`, `/marry` или `Пожениться`.\n\n"
                    "💵 Стоимость проведения свадьбы: 1000$.",
                )

        elif text_lower.startswith(("/pay", "дать", "перевести")):
            if "reply_to_message" not in msg:
                await send_message(
                    chat_id,
                    "⚠️ Ответьте (Reply) на сообщение игрока, которому хотите перевести деньги!\nПример: `/pay 500`",
                )
                return

            parts = text.split()
            if len(parts) < 2 or not parts[1].isdigit():
                await send_message(
                    chat_id, "⚠️ Укажите сумму для перевода! Пример: `/pay 500`"
                )
                return

            amount = int(parts[1])
            if amount <= 0:
                await send_message(chat_id, "❌ Сумма перевода должна быть больше 0!")
                return

            target_user = msg["reply_to_message"]["from"]
            target_id = target_user["id"]
            target_name = target_user.get("first_name", "Игрок")

            if target_id == user_id or target_user.get("is_bot"):
                await send_message(
                    chat_id, "❌ Нельзя переводить деньги самому себе или ботам!"
                )
                return

            cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
            balance = cursor.fetchone()[0]

            if balance < amount:
                await send_message(
                    chat_id, f"❌ Недостаточно средств! Ваш баланс: {balance}$."
                )
                return

            get_user(target_id, target_name)
            update_balance(user_id, -amount)
            update_balance(target_id, amount)

            await send_message(
                chat_id,
                f"💸 *{first_name}* перевел *{amount}$* игроку *{target_name}*!",
            )

        elif text_lower in ["ограбить", "/rob", "грабеж"]:
            if "reply_to_message" not in msg:
                await send_message(
                    chat_id,
                    "⚠️ Ответьте (Reply) на сообщение игрока, которого хотите ограбить!",
                )
                return

            target_user = msg["reply_to_message"]["from"]
            target_id = target_user["id"]
            target_name = target_user.get("first_name", "Игрок")

            if target_id == user_id or target_user.get("is_bot"):
                await send_message(chat_id, "❌ Нельзя грабить самого себя или ботов!")
                return

            curr_time = int(time.time())
            cursor.execute(
                "SELECT last_rob, balance FROM users WHERE user_id = ?", (user_id,)
            )
            last_rob, my_balance = cursor.fetchone()

            if curr_time - last_rob < 600:
                rem = 600 - (curr_time - last_rob)
                await send_message(
                    chat_id,
                    f"🚨 Вы скрываетесь от полиции! Попробуйте снова через {int(rem // 60)} мин. {int(rem % 60)} сек.",
                )
                return

            get_user(target_id, target_name)
            cursor.execute("SELECT balance FROM users WHERE user_id = ?", (target_id,))
            target_balance = cursor.fetchone()[0]

            if target_balance < 200:
                await send_message(
                    chat_id, f"❌ У *{target_name}* слишком мало денег (меньше 200$)!"
                )
                return

            cursor.execute(
                "UPDATE users SET last_rob = ? WHERE user_id = ?", (curr_time, user_id)
            )
            conn.commit()

            if random.random() < 0.45:
                stolen_amount = random.randint(100, min(1000, int(target_balance * 0.3)))
                update_balance(target_id, -stolen_amount)
                update_balance(user_id, stolen_amount)
                await send_message(
                    chat_id,
                    f"🥷 *УСПЕХ!* *{first_name}* вытащил из кармана *{target_name}* {stolen_amount}$!",
                )
            else:
                fine = random.randint(100, 300)
                update_balance(user_id, -fine)
                await send_message(
                    chat_id,
                    f"🚨 *ПРОВАЛ!* *{first_name}* попался при попытке ограбления и уплатил штраф {fine}$!",
                )

        elif text_lower.startswith(("дуэль", "/duel")):
            if "reply_to_message" not in msg:
                await send_message(
                    chat_id,
                    "⚠️ Ответьте (Reply) на сообщение игрока для вызова на дуэль!\nПример: `дуэль 300`",
                )
                return

            parts = text.split()
            bet = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 200

            if bet < 50:
                await send_message(chat_id, "❌ Минимальная ставка: 50$!")
                return

            target_user = msg["reply_to_message"]["from"]
            target_id = target_user["id"]
            target_name = target_user.get("first_name", "Игрок")

            if target_id == user_id or target_user.get("is_bot"):
                await send_message(chat_id, "❌ Ошибка вызова!")
                return

            cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
            if cursor.fetchone()[0] < bet:
                await send_message(chat_id, "❌ У вас недостаточно денег!")
                return

            get_user(target_id, target_name)
            cursor.execute("SELECT balance FROM users WHERE user_id = ?", (target_id,))
            if cursor.fetchone()[0] < bet:
                await send_message(
                    chat_id, f"❌ У *{target_name}* недостаточно средств!"
                )
                return

            pending_duels[target_id] = {
                "challenger_id": user_id,
                "challenger_name": first_name,
                "bet": bet,
            }

            await send_message(
                chat_id,
                f"⚔️ *ПОЕДИНОК!*\n\n"
                f"*{first_name}* вызывает на дуэль *{target_name}*!\n"
                f"💰 Ставка: *{bet}$*.\n\n"
                f"{target_name}, вы принимаете вызов?",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "⚔️ Принять",
                                "callback_data": f"accept_duel_{user_id}",
                            },
                            {
                                "text": "🏳️ Отказаться",
                                "callback_data": f"decline_duel_{user_id}",
                            },
                        ]
                    ]
                },
            )

        elif any(
            cmd in text_lower
            for cmd in ["брак", "/marry", "пожениться", "выйти замуж"]
        ):
            if "reply_to_message" not in msg:
                await send_message(
                    chat_id,
                    "⚠️ Ответьте (Reply) на сообщение человека, с кем хотите сыграть свадьбу!",
                )
                return

            target_user = msg["reply_to_message"]["from"]
            target_id = target_user["id"]
            target_name = target_user.get("first_name", "Игрок")

            if target_user.get("is_bot") or target_id == user_id:
                await send_message(chat_id, "❌ Неверный выбор партнера!")
                return

            cursor.execute(
                "SELECT balance, spouse_id FROM users WHERE user_id = ?",
                (user_id,),
            )
            balance, spouse_id = cursor.fetchone()

            if spouse_id or balance < 1000:
                await send_message(
                    chat_id, "❌ Вы уже в браке или у вас нет 1000$!"
                )
                return

            get_user(target_id, target_name)
            cursor.execute(
                "SELECT spouse_id FROM users WHERE user_id = ?", (target_id,)
            )
            if cursor.fetchone()[0]:
                await send_message(
                    chat_id, f"❌ {target_name} уже состоит в браке!"
                )
                return

            pending_proposals[target_id] = user_id
            await send_message(
                chat_id,
                f"💍 *{first_name}* делает предложение руки и сердца *{target_name}*!\n\n"
                f"{target_name}, вы согласны?",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "✅ Согласиться",
                                "callback_data": f"accept_marry_{user_id}",
                            },
                            {
                                "text": "❌ Отказать",
                                "callback_data": f"decline_marry_{user_id}",
                            },
                        ]
                    ]
                },
            )

        elif text == "🎰 Казино":
            await send_message(
                chat_id,
                "Добро пожаловать в казино! Выберите игру:",
                reply_markup=casino_menu_keyboard,
            )

        elif text == "🎁 Ежедневный бонус":
            curr_time = int(time.time())
            cursor.execute(
                "SELECT last_bonus FROM users WHERE user_id = ?", (user_id,)
            )
            last_bonus = cursor.fetchone()[0]

            if curr_time - last_bonus >= 86400:
                cursor.execute(
                    "UPDATE users SET balance = balance + 500, last_bonus = ? WHERE user_id = ?",
                    (curr_time, user_id),
                )
                conn.commit()
                await send_message(chat_id, "🎉 Вы получили ежедневный бонус 500$!")
            else:
                rem = 86400 - (curr_time - last_bonus)
                await send_message(
                    chat_id,
                    f"⏳ Бонус доступен через {int(rem // 3600)} ч. {int((rem % 3600) // 60)} мин.",
                )

        elif text == "📺 Реклама (+300$)":
            curr_time = int(time.time())
            cursor.execute(
                "SELECT last_ad FROM users WHERE user_id = ?", (user_id,)
            )
            last_ad = cursor.fetchone()[0]

            if curr_time - last_ad >= 3600:
                cursor.execute(
                    "UPDATE users SET balance = balance + 300, last_ad = ? WHERE user_id = ?",
                    (curr_time, user_id),
                )
                conn.commit()
                await send_message(
                    chat_id, "📺 Спасибо за просмотр рекламы! Вам начислено +300$."
                )
            else:
                rem = 3600 - (curr_time - last_ad)
                await send_message(
                    chat_id,
                    f"⏳ Следующий просмотр через {int((rem % 3600) // 60)} мин. {int(rem % 60)} сек.",
                )

    elif "callback_query" in update:
        call = update["callback_query"]
        chat_id = call["message"]["chat"]["id"]
        message_id = call["message"]["message_id"]
        data = call.get("data")

        if data == "help_jobs":
            await edit_message_text(
                chat_id,
                message_id,
                "💼 *Работа и Доход:*\n\n"
                "• *Работа*: Нажмите кнопку «💼 Работа» в меню, чтобы выбрать профессию и получать ЗП каждый час.\n"
                "• *Ежедневный бонус*: Раз в 24 часа дает +500$.\n"
                "• *Просмотр рекламы*: Раз в час дает +300$.",
                reply_markup=help_keyboard,
            )

        elif data == "help_rp":
            await edit_message_text(
                chat_id,
                message_id,
                "⚔️ *RP Взаимодействия (в чатах через Reply):*\n\n"
                "• *Перевод денег*: Ответьте на сообщение игрока: `/pay [сумма]` (или `дать 500`).\n"
                "• *Ограбление*: Ответьте на сообщение игрока: `ограбить` или `/rob`. Шанс успеха 45%. Кулдаун: 10 мин.\n"
                "• *Дуэль*: Ответьте на сообщение игрока: `дуэль [ставка]` (или `/duel 200`). Победитель забирает банк.\n"
                "• *Свадьба*: Ответьте на сообщение игрока: `Брак` или `/marry`. Стоимость: 1000$.",
                reply_markup=help_keyboard,
            )

        elif data == "help_casino":
            await edit_message_text(
                chat_id,
                message_id,
                "🎰 *Казино и Мини-игры:*\n\n"
                "• *Рулетка*: Ставки на Красное (x2), Чёрное (x2) или Зеро (x14). Мин. ставка: 100$.\n"
                "• *Игра 21 (Блэкджек)*: Наберите как можно ближе к 21 очку, но не переберите. Ставка: 200$.",
                reply_markup=help_keyboard,
            )

        elif data.startswith("job_set_"):
            new_job = data.replace("job_set_", "")
            cursor.execute(
                "UPDATE users SET job = ? WHERE user_id = ?", (new_job, user_id)
            )
            conn.commit()
            await answer_callback_query(
                call["id"], f"🎉 Вы устроились на работу: {new_job}!"
            )
            await edit_message_text(
                chat_id,
                message_id,
                f"✅ Вы успешно устроились на работу: {new_job}!\nТеперь вы можете получать зарплату каждый час.",
            )

        elif data == "job_work":
            curr_time = int(time.time())
            cursor.execute(
                "SELECT job, last_work FROM users WHERE user_id = ?", (user_id,)
            )
            job, last_work = cursor.fetchone()

            if job == "Безработный":
                await answer_callback_query(
                    call["id"], "❌ Сначала устройтесь на работу!"
                )
                return

            if curr_time - last_work >= 3600:
                pay = JOBS[job]["pay"]
                cursor.execute(
                    "UPDATE users SET balance = balance + ?, last_work = ? WHERE user_id = ?",
                    (pay, curr_time, user_id),
                )
                conn.commit()
                await answer_callback_query(
                    call["id"], f"💰 Вы отработали смену и получили {pay}$!"
                )
                await edit_message_text(
                    chat_id,
                    message_id,
                    f"⚙️ Вы успешно отработали смену на должности {job}!\n💵 Заработок: +{pay}$",
                )
            else:
                rem = 3600 - (curr_time - last_work)
                await answer_callback_query(
                    call["id"],
                    f"⏳ Вы устали! Отдохните ещё {int((rem % 3600) // 60)} мин.",
                )

        elif data.startswith("accept_duel_"):
            challenger_id = int(data.replace("accept_duel_", ""))

            if user_id in pending_duels and pending_duels[user_id]["challenger_id"] == challenger_id:
                duel = pending_duels[user_id]
                bet = duel["bet"]
                challenger_name = duel["challenger_name"]
                my_name = call["from"].get("first_name", "Игрок")

                cursor.execute("SELECT balance FROM users WHERE user_id = ?", (challenger_id,))
                c_bal = cursor.fetchone()[0]
                cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
                m_bal = cursor.fetchone()[0]

                if c_bal < bet or m_bal < bet:
                    await edit_message_text(
                        chat_id, message_id, "❌ У одного из участников недостаточно средств!"
                    )
                    del pending_duels[user_id]
                    return

                winner_id, loser_id = (user_id, challenger_id) if random.choice([True, False]) else (challenger_id, user_id)
                winner_name = my_name if winner_id == user_id else challenger_name
                loser_name = challenger_name if winner_id == user_id else my_name

                update_balance(winner_id, bet)
                update_balance(loser_id, -bet)
                del pending_duels[user_id]

                await edit_message_text(
                    chat_id,
                    message_id,
                    f"🔫 *ДУЭЛЬ СОСТОЯЛАСЬ!*\n\n💥 Прогремел выстрел...\n"
                    f"🏆 Победитель: *{winner_name}* (+{bet}$)\n"
                    f"💀 Проигравший: *{loser_name}* (-{bet}$)",
                )

        elif data.startswith("decline_duel_"):
            if user_id in pending_duels:
                del pending_duels[user_id]
                await edit_message_text(
                    chat_id, message_id, "🏳️ Игрок отклонил вызов на дуэль."
                )

        elif data.startswith("accept_marry_"):
            proposer_id = int(data.replace("accept_marry_", ""))

            if user_id in pending_proposals and pending_proposals[user_id] == proposer_id:
                cursor.execute(
                    "UPDATE users SET balance = balance - 1000, spouse_id = ? WHERE user_id = ?",
                    (user_id, proposer_id),
                )
                cursor.execute(
                    "UPDATE users SET spouse_id = ? WHERE user_id = ?",
                    (proposer_id, user_id),
                )
                conn.commit()

                del pending_proposals[user_id]
                await edit_message_text(
                    chat_id,
                    message_id,
                    f"🎉 ПОЗДРАВЛЯЕМ!\n\n✨ {get_user_name(proposer_id)} и {get_user_name(user_id)} теперь официально состоят в браке! 💍❤️",
                )

        elif data.startswith("decline_marry_"):
            if user_id in pending_proposals:
                del pending_proposals[user_id]
                await edit_message_text(
                    chat_id, message_id, "💔 Предложение руки и сердца было отклонено."
                )

        elif data == "divorce":
            cursor.execute(
                "SELECT spouse_id FROM users WHERE user_id = ?", (user_id,)
            )
            spouse_id = cursor.fetchone()[0]

            if spouse_id:
                cursor.execute(
                    "UPDATE users SET spouse_id = 0 WHERE user_id = ?",
                    (user_id,),
                )
                cursor.execute(
                    "UPDATE users SET spouse_id = 0 WHERE user_id = ?",
                    (spouse_id,),
                )
                conn.commit()
                await edit_message_text(
                    chat_id, message_id, "💔 Вы официально развелись."
                )

        elif data == "menu_roulette":
            await edit_message_text(
                chat_id,
                message_id,
                "🎰 Рулетка\nМинимальная ставка: 100$.\nСделайте вашу ставку:",
                reply_markup=roulette_keyboard,
            )

        elif data == "menu_21":
            cursor.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            )
            if cursor.fetchone()[0] < 200:
                await answer_callback_query(
                    call["id"], "❌ Для игры нужно минимум 200$."
                )
                return

            player_hand = [get_card(), get_card()]
            dealer_hand = [get_card()]
            games_21[user_id] = {
                "player": player_hand,
                "dealer": dealer_hand,
                "bet": 200,
            }

            p_score = calculate_score(player_hand)
            await edit_message_text(
                chat_id,
                message_id,
                f"🃏 Игра 21 (Ставка: 200$)\n\nВаши карты: {player_hand} ({p_score})\nКарта дилера: {dealer_hand}",
                reply_markup=game21_keyboard,
            )

        elif data.startswith("roulette_"):
            cursor.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            )
            if cursor.fetchone()[0] < 100:
                await answer_callback_query(call["id"], "❌ Недостаточно средств!")
                return

            choice = data.split("_")[1]
            spin = random.choices(
                ["red", "black", "zero"], weights=[48, 48, 4], k=1
            )[0]

            if choice == spin:
                win_mult = 14 if spin == "zero" else 2
                win_amount = 100 * win_mult - 100
                update_balance(user_id, win_amount)
                result_text = f"🎉 ВЫИГРЫШ! Выпало {spin.upper()}. +{win_amount}$!"
            else:
                update_balance(user_id, -100)
                result_text = f"🔻 Проигрыш! Выпало {spin.upper()}. -100$."

            await answer_callback_query(call["id"], result_text)
            await edit_message_text(
                chat_id, message_id, f"🎰 Рулетка:\n{result_text}"
            )

        elif data in ["21_hit", "21_stand"]:
            if user_id not in games_21:
                await answer_callback_query(
                    call["id"], "⚠️ Игра уже завершена!"
                )
                return

            game = games_21[user_id]

            if data == "21_hit":
                game["player"].append(get_card())
                p_score = calculate_score(game["player"])

                if p_score > 21:
                    update_balance(user_id, -game["bet"])
                    await edit_message_text(
                        chat_id,
                        message_id,
                        f"💥 Перебор! Ваши карты: {game['player']} ({p_score} очков).\nПроигрыш -{game['bet']}$.",
                    )
                    del games_21[user_id]
                else:
                    await edit_message_text(
                        chat_id,
                        message_id,
                        f"Ваши карты: {game['player']} ({p_score} очков).\nВзять ещё или хватит?",
                        reply_markup=game21_keyboard,
                    )

            elif data == "21_stand":
                while calculate_score(game["dealer"]) < 17:
                    game["dealer"].append(get_card())

                p_score = calculate_score(game["player"])
                d_score = calculate_score(game["dealer"])

                text = f"🏁 Итоги:\nИгрок: {game['player']} ({p_score})\nДилер: {game['dealer']} ({d_score})\n\n"

                if d_score > 21 or p_score > d_score:
                    update_balance(user_id, game["bet"])
                    text += f"🎉 Вы выиграли +{game['bet']}$!"
                elif p_score < d_score:
                    update_balance(user_id, -game["bet"])
                    text += f"🔻 Вы проиграли -{game['bet']}$."
                else:
                    text += "🤝 Ничья!"

                await edit_message_text(chat_id, message_id, text)
                del games_21[user_id]


# --- 5. ГЛАВНЫЙ ЦИКЛ БЕЗ ЗАДЕРЖЕК ---
async def main():
    global http_session
    # Создаем асинхронную HTTP-сессию с длинным соединением
    connector = aiohttp.TCPConnector(ssl=False)
    http_session = aiohttp.ClientSession(connector=connector)

    await api_request("deleteWebhook", {"drop_pending_updates": True})
    print("🚀 Быстрый Казино-бот запущен! Ошибки HTTP устранены.")

    offset = 0

    while True:
        try:
            res = await api_request(
                "getUpdates", {"offset": offset, "timeout": 20}
            )
            if res.get("ok"):
                for update in res.get("result", []):
                    offset = update["update_id"] + 1
                    # Обработка каждой команды происходит в фоновом потоке
                    asyncio.create_task(handle_update(update))
        except Exception:
            await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")
