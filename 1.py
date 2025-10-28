import telebot
from telebot import types
from types import SimpleNamespace as _SimpleNS
import sqlite3
from datetime import datetime, timedelta
import random
import string
import os
import csv
import config
import photos
import threading
import pytz
import re  # Added for phone validation
import requests
import uuid
import json

# Безопасная загрузка минимального холда
try:
    MIN_HOLD_MINUTES = int(getattr(config, 'MIN_HOLD_MINUTES', 54))
except Exception:
    MIN_HOLD_MINUTES = 54

def adapt_datetime(dt):
    return dt.isoformat()

sqlite3.register_adapter(datetime, adapt_datetime)

def convert_datetime(s):
    if isinstance(s, bytes):
        s = s.decode('utf-8')
    return datetime.fromisoformat(s)

sqlite3.register_converter("DATETIME", convert_datetime)

bot = telebot.TeleBot(config.BOT_TOKEN)

conn = sqlite3.connect('bot.db', check_same_thread=False, detect_types=sqlite3.PARSE_DECLTYPES)
cursor = conn.cursor()

# Initialize database tables
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    reputation REAL DEFAULT 10.0,
    balance REAL DEFAULT 0.0,
    subscription_type TEXT,
    subscription_end DATETIME,
    referral_code TEXT,
    referrals_count INTEGER DEFAULT 0,
    profit_level TEXT DEFAULT 'новичок',
    card_number TEXT,
    cvv TEXT,
    card_balance REAL DEFAULT 0.0,
    card_status TEXT DEFAULT 'inactive',
    card_password TEXT,
    card_activation_date DATETIME,
    phone_number TEXT,
    last_activity DATETIME,
    api_token TEXT,
    block_reason TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    phone_number TEXT UNIQUE,
    added_time DATETIME,
    type TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS working (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    phone_number TEXT UNIQUE,
    start_time DATETIME,
    admin_id INTEGER,
    type TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS successful (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    phone_number TEXT,
    hold_time TEXT,
    acceptance_time DATETIME,
    flight_time DATETIME,
    type TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS blocked (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    phone_number TEXT,
    type TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS referrals (
    referer_id INTEGER,
    referee_id INTEGER,
    PRIMARY KEY (referer_id, referee_id)
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS withdraw_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    status TEXT DEFAULT 'pending',
    created_at DATETIME,
    paid_at DATETIME
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS deposit_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    created_at DATETIME NOT NULL,
    request_id INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id)
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT,
    timestamp DATETIME
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS admin_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER,
    action TEXT,
    timestamp DATETIME
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS status (
    key TEXT PRIMARY KEY,
    value TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS card_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    timestamp DATETIME,
    type TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER,
    to_user_id INTEGER,
    amount REAL,
    timestamp DATETIME
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    sub_type TEXT,
    amount REAL,
    invoice_id TEXT,
    payload TEXT,
    status TEXT DEFAULT 'pending',
    transaction_id TEXT
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id INTEGER,
    amount REAL,
    unique_code TEXT UNIQUE,
    description TEXT,
    password TEXT,
    image_file_id TEXT,
    require_subs TEXT,
    require_premium INTEGER DEFAULT 0,
    activated_by INTEGER,
    activated_at DATETIME
)
''')

# Add columns if missing
try:
    cursor.execute("ALTER TABLE checks ADD COLUMN unique_code TEXT UNIQUE")
    conn.commit()
except sqlite3.OperationalError:
    pass

try:
    cursor.execute("ALTER TABLE checks ADD COLUMN description TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass

try:
    cursor.execute("ALTER TABLE checks ADD COLUMN password TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass

try:
    cursor.execute("ALTER TABLE checks ADD COLUMN image_file_id TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass

try:
    cursor.execute("ALTER TABLE checks ADD COLUMN require_subs TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass

try:
    cursor.execute("ALTER TABLE checks ADD COLUMN require_premium INTEGER DEFAULT 0")
    conn.commit()
except sqlite3.OperationalError:
    pass

try:
    cursor.execute("ALTER TABLE checks ADD COLUMN activated_by INTEGER")
    conn.commit()
except sqlite3.OperationalError:
    pass

try:
    cursor.execute("ALTER TABLE checks ADD COLUMN activated_at DATETIME")
    conn.commit()
except sqlite3.OperationalError:
    pass

try:
    cursor.execute("ALTER TABLE payments ADD COLUMN payload TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass

try:
    cursor.execute("ALTER TABLE withdraw_requests ADD COLUMN created_at DATETIME")
    conn.commit()
except sqlite3.OperationalError:
    pass

try:
    cursor.execute("ALTER TABLE withdraw_requests ADD COLUMN paid_at DATETIME")
    conn.commit()
except sqlite3.OperationalError:
    pass

cursor.execute("INSERT OR IGNORE INTO status (key, value) VALUES ('work_status', 'Full work 🟢')")
conn.commit()

# Add initial admin
cursor.execute("INSERT OR IGNORE INTO admins (id) VALUES (?)", (config.ADMIN_IDS[0],))
conn.commit()

pending_activations = {}  # To store admin_id for pending activations
pending_timers = {}  # To store timers for cancellation

# Новый словарь для pending steps (чтобы не использовать встроенный next_step_handler и избежать запоминания)
pending_steps = {}

def register_next_step(chat_id, handler, *args):
    pending_steps[chat_id] = (handler, args)

def clear_pending_step(chat_id):
    if chat_id in pending_steps:
        del pending_steps[chat_id]

def is_subscribed(user_id):
    try:
        member = bot.get_chat_member(config.CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

def generate_referral_code(user_id):
    return f"ref_{user_id}"

def get_profit_level(referrals, is_admin=False):
    if is_admin:
        return 'ADMIN FAX'
    if referrals < 10:
        return 'новичок'
    elif referrals < 30:
        return 'продвинутый'
    elif referrals < 60:
        return 'воркер'
    elif referrals < 90:
        return 'VIP WORK'
    else:
        return 'VIP WORK'

def is_admin(user_id):
    cursor.execute("SELECT * FROM admins WHERE id = ?", (user_id,))
    return cursor.fetchone() is not None

tz = pytz.timezone('Europe/Moscow')

def log_action(user_id, action):
    cursor.execute("INSERT INTO logs (user_id, action, timestamp) VALUES (?, ?, ?)", (user_id, action, datetime.now(tz)))
    conn.commit()

def log_admin_action(admin_id, action):
    cursor.execute("INSERT INTO admin_logs (admin_id, action, timestamp) VALUES (?, ?, ?)", (admin_id, action, datetime.now(tz)))
    conn.commit()

def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row))
    return None

def update_user(user_id, **kwargs):
    set_clause = ', '.join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [user_id]
    cursor.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
    conn.commit()

def get_queue():
    cursor.execute("SELECT * FROM queue ORDER BY added_time ASC")
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]

def get_user_queue(user_id):
    cursor.execute("SELECT * FROM queue WHERE user_id = ? ORDER BY added_time ASC", (user_id,))
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]

def get_working(user_id=None):
    if user_id:
        cursor.execute("SELECT * FROM working WHERE user_id = ?", (user_id,))
    else:
        cursor.execute("SELECT * FROM working")
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]

def get_successful(user_id=None):
    if user_id:
        cursor.execute("SELECT * FROM successful WHERE user_id = ?", (user_id,))
    else:
        cursor.execute("SELECT * FROM successful")
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]

def get_blocked(user_id=None):
    if user_id:
        cursor.execute("SELECT * FROM blocked WHERE user_id = ?", (user_id,))
    else:
        cursor.execute("SELECT * FROM blocked")
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]

def get_status(key):
    cursor.execute("SELECT value FROM status WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row[0] if row else None

def set_status(key, value):
    cursor.execute("REPLACE INTO status (key, value) VALUES (?, ?)", (key, value))
    conn.commit()

def generate_card_number():
    return ''.join(random.choices(string.digits, k=16))

def generate_cvv():
    return ''.join(random.choices(string.digits, k=3))

def generate_api_token(user_id):
    return f"{user_id}:{str(uuid.uuid4())}"

def calculate_hold(accept_time, flight_time):
    if isinstance(accept_time, str):
        accept_time = datetime.fromisoformat(accept_time)
    if isinstance(flight_time, str):
        flight_time = datetime.fromisoformat(flight_time)
    delta = flight_time - accept_time
    minutes = delta.total_seconds() / 60
    if minutes >= MIN_HOLD_MINUTES:
        hours = int(minutes // 60)
        mins = int(minutes % 60)
        return f"{hours:02d}:{mins:02d}"
    return None

def get_price_increase(sub_type):
    if not sub_type:
        return config.PRICES['hour'], config.PRICES['30min']
    sub = config.SUBSCRIPTIONS.get(sub_type, {})
    return sub.get('price_increase_hour', 0), sub.get('price_increase_30min', 0)

def sort_queue(queue):
    def key_func(item):
        user = get_user(item['user_id'])
        rep = user['reputation']
        sub = user['subscription_type']
        priority = 0
        if sub == 'VIP Nexus':
            priority = 4
        elif sub == 'Prime Plus':
            priority = 3
        elif sub == 'Gold Tier':
            priority = 2
        elif sub == 'Elite Access':
            priority = 1
        return (-priority, -rep, item['added_time'])
    return sorted(queue, key=key_func)

def show_main_menu(chat_id, edit_message_id=None):
    clear_pending_step(chat_id)  # Очищаем pending при показе главного меню
    user = get_user(chat_id)
    if not user:
        return
    username = user['username']
    status = get_status('work_status')
    reputation = user['reputation']
    balance = user['balance']
    queue_count = len(get_queue())
    user_queue_count = len(get_user_queue(chat_id))
    caption = f"@{username} | Full Work\n➢Статус ворка: {status}\n➣Репутация: {reputation}\n➢Баланс: {balance}\n╓Общая очередь: {queue_count}\n║\n╚Твои номера в очереди: {user_queue_count}"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Добавить номер 🚀", callback_data="add_number"), types.InlineKeyboardButton("Мои номера 📱", callback_data="my_numbers"))
    markup.add(types.InlineKeyboardButton("Очередь 🔄", callback_data="queue"), types.InlineKeyboardButton("Статистика 📊", callback_data="stats"))
    markup.row(types.InlineKeyboardButton("Мой профиль 👤", callback_data="profile"))
    if edit_message_id:
        bot.edit_message_media(chat_id=chat_id, message_id=edit_message_id, media=types.InputMediaPhoto(photos.PHOTOS['start'], caption=caption), reply_markup=markup)
    else:
        bot.send_photo(chat_id, photos.PHOTOS['start'], caption=caption, reply_markup=markup)

@bot.message_handler(commands=['start'])
def handle_start(message):
    clear_pending_step(message.chat.id)  # Очищаем pending
    param = message.text.split()[1] if len(message.text.split()) > 1 else ""
    if param.startswith("check_"):
        handle_check_activation(message, param[6:])
        return
    if message.from_user.username is None:
        bot.send_message(message.chat.id, "📼Ваш username не определён, вам нужно установить username, перейдите по этому пути:\n\n⚙️Настройки->Имя пользователя->Указываете username.\n\n🌐После установки username, пришлите команду /start")
        return
    user_id = message.chat.id
    username = message.from_user.username or str(user_id)
    ref = param if param and param.startswith('ref_') else None
    user = get_user(user_id)
    if not user:
        referral_code = generate_referral_code(user_id)
        cursor.execute("INSERT INTO users (id, username, referral_code, last_activity, profit_level) VALUES (?, ?, ?, ?, ?)", (user_id, username, referral_code, datetime.now(tz), 'новичок'))
        conn.commit()
        if ref:
            referer_id = int(ref[4:])
            if referer_id != user_id:
                cursor.execute("INSERT OR IGNORE INTO referrals (referer_id, referee_id) VALUES (?, ?)", (referer_id, user_id))
                conn.commit()
                referer = get_user(referer_id)
                update_user(referer_id, balance=referer['balance'] + 0.5, referrals_count=referer['referrals_count'] + 1)
                referrals = get_user(referer_id)['referrals_count']
                profit = get_profit_level(referrals, is_admin=is_admin(referer_id))
                update_user(referer_id, profit_level=profit)
                bot.send_message(referer_id, f"+$0.5 за нового реферала [{user_id}]")
                bot.send_photo(referer_id, photos.PHOTOS['new_profit'])
    else:
        update_user(user_id, last_activity=datetime.now(tz))
    if not is_subscribed(user_id):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Подписаться 📢", url="https://t.me/NafuzorTime"))
        markup.add(types.InlineKeyboardButton("Подписаться 📢", url="https://t.me/lixcuk_robot_prime"))
        markup.add(types.InlineKeyboardButton("Проверить ✅", callback_data="check_sub"))
     
        bot.send_message(user_id, "Добро пожаловать, подпишись чтобы бот работал!.", reply_markup=markup)
    else:
        show_main_menu(user_id)
        
        
        
        

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def check_sub(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    if is_subscribed(call.from_user.id):
        bot.delete_message(call.message.chat.id, call.message.message_id)
        show_main_menu(call.message.chat.id)
    else:
        bot.answer_callback_query(call.id, "Вы еще не подписаны на все каналы!", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def back_main(call):
    clear_pending_step(call.message.chat.id)
    show_main_menu(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "add_number")
def add_number_type_choice(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    caption = "Выберите тип номера"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("макс🖥️", callback_data="add_max"), types.InlineKeyboardButton("вц💻", callback_data="add_vc"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="back_main"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['add_number'], caption=caption), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ["add_max", "add_vc"])
def add_number(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending перед новым input
    number_type = 'max' if call.data == "add_max" else 'vc'
    if number_type == 'max':
        caption = "Введите номер в формате +7XXXXXXXXXX"
    else:
        caption = "Введите номер в формате 9XXXXXXXXX"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="add_number"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['add_number'], caption=caption), reply_markup=markup)
    register_next_step(call.message.chat.id, process_add_number, call.message.message_id, number_type)

def process_add_number(message, message_id=None, number_type=None):
    phone = message.text.strip()
    if number_type == 'max':
        if not re.match(r'\+7\d{10}', phone):
            bot.send_message(message.chat.id, "Неверный формат. Попробуйте снова.")
            add_number_type_choice(_SimpleNS(message=message, from_user=message.from_user, data="add_number"))
            return
    else:
        if len(phone) != 10 or not phone.isdigit() or not phone.startswith('9'):
            bot.send_message(message.chat.id, "Неверный формат. Попробуйте снова.")
            add_number_type_choice(_SimpleNS(message=message, from_user=message.from_user, data="add_number"))
            return
    cursor.execute("SELECT * FROM queue WHERE phone_number = ?", (phone,))
    if cursor.fetchone():
        bot.send_message(message.chat.id, "Номер уже добавлен.")
        show_main_menu(message.chat.id)
        return
    cursor.execute("INSERT INTO queue (user_id, phone_number, added_time, type) VALUES (?, ?, ?, ?)", (message.chat.id, phone, datetime.now(tz), number_type))
    conn.commit()
    log_action(message.chat.id, f"Добавлен номер {phone} типа {number_type}")
    show_main_menu(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "my_numbers")
def my_numbers(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    caption = "Мои номера"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("В работе ⚙️", callback_data="my_working"), types.InlineKeyboardButton("Ожидает ⏳", callback_data="my_queue"))
    markup.add(types.InlineKeyboardButton("Успешные ✅", callback_data="my_successful"), types.InlineKeyboardButton("Блок 🛑", callback_data="my_blocked"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="back_main"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['my_numbers'], caption=caption), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("my_"))
def show_my_list(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    data = call.data
    items = None
    title = None
    if data == "my_queue":
        items = get_user_queue(call.message.chat.id)
        title = "Ожидает"
    elif data == "my_working":
        items = get_working(call.message.chat.id)
        title = "В работе"
    elif data == "my_successful":
        items = get_successful(call.message.chat.id)
        title = "Успешные"
    elif data == "my_blocked":
        items = get_blocked(call.message.chat.id)
        title = "Блок"
    else:
        bot.answer_callback_query(call.id, "Неверный запрос")
        return
    caption = f"{title}\n" + "\n".join(f"{item['phone_number']} ({item['type']})" for item in items) if items else f"{title}: Пусто"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="my_numbers"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "queue")
def show_queue(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    user = get_user(call.message.chat.id)
    sub = user['subscription_type']
    if sub in ['Gold Tier', 'Prime Plus', 'VIP Nexus']:
        queue = sort_queue(get_queue())
        caption = "Очередь:\n" + "\n".join(f"{item['phone_number']} ({item['type']})" for item in queue) if queue else "Очередь пуста"
    else:
        caption = f"Общая очередь: {len(get_queue())}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="back_main"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "stats")
def show_stats(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Данная функция не доступна", show_alert=True)
        return
    stats = get_successful()
    caption = "Статистика:\n" + "\n".join(f"{get_user(item['user_id'])['username']}-{item['phone_number']} ({item['type']})-холд: {item['hold_time']}" for item in stats if item['hold_time'])
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="back_main"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "profile")
def show_profile(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending при входе в профиль
    user = get_user(call.message.chat.id)
    username = user['username']
    reputation = user['reputation']
    sub = user['subscription_type'] or ""
    price_hour, price_30 = get_price_increase(sub)
    price_text = f"час-{price_hour}$ 30мин-{price_30}$" if sub else ""
    balance = user['balance']
    caption = f"▶ Юзернейм: @{username}\n╓ Репутация: {reputation}\n║\n╚ Подписка: {sub}\n▶ Прайс: {price_text}\n╓ Баланс: ${balance}"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Купить подписку 💳", callback_data="buy_sub"), types.InlineKeyboardButton("Реферальная система 🔗", callback_data="referral"))
    markup.add(types.InlineKeyboardButton("Карта 💳", callback_data="card"), types.InlineKeyboardButton("Правила 📜", callback_data="rules"))
    markup.add(types.InlineKeyboardButton("Создать чек 🧾", callback_data="create_check_menu"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="back_main"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['profile'], caption=caption), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "rules")
def show_rules(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    rules_text = "<blockquote>Основные правила бота\n1️⃣ Что нельзя делать ни в коем случае!\n‼️‼️ ЮЗЫ НЕ МЕНЯТЬ, КТО БЫ ВАМ НИ ПИСАЛ! ЧТО БЫ ВАМ НИ ПИСАЛИ! ‼️‼️\n‼️‼️ СМЕНИТЕ ЮЗ – ОСТАНЕТЕСЬ БЕЗ ВЫПЛАТЫ! БУДЕТЕ ПОТОМ ЖАЛОВАТЬСЯ! ‼️‼️\n‼️‼️ ЕСЛИ ВАС ПО КАКОЙ-ТО ПРИЧИНЕ ЗАБАНИЛИ (РЕКЛАМА, СКАМ, ПЕРЕЛИВ И Т.Д.) – ЛИШЕНИЕ ВЫПЛАТЫ! ‼️‼️\n\n2️⃣ Если ваш номер отстоял, например, 1 час, вам не нужно делать никаких отчётов.\nМы сами скинем табель в эту группу.\nЧтобы посмотреть, сколько именно отстоял ваш номер, введите команду /hold – она покажет номер и холд! 📊\n\n3️⃣ Как пользоваться ботом?\n\nНажимаете кнопку «Добавить номер».\n\nВписываете номер в формате 9XXXXXXXXX.\n\nЖдёте, пока ваш номер возьмут в работу.\n\nПосле этого вам придёт сообщение:\n\n✆ (Ваш номер) ЗАПРОС АКТИВАЦИИ\n✎ Ограничение времени активации: 2 минуты\n✔ ТВОЙ КОД: (здесь будет код от скупа)\n\nНиже будут две кнопки: «Ввёл» и «Скип».\n\nЕсли нажали «Ввёл», номер перейдёт в раздел «В работе» – это значит, что вы ввели код. ✅\n\nЕсли нажали «Скип», номер удалится из очереди и не будет активирован. ❌\n\n4️⃣ Как узнать статус вашего номера?\nНажимаете кнопку «Мои номера» и выбираете нужный пункт (всего 4):\n\n🔹 В работе – номер ещё стоит.\n🔹 Ожидает – номер в очереди, его ещё не взяли в работу.\n🔹 Успешные – номер с холдом более 54 минут (будет выплата). 💰\n🔹 Блок – номер слетел без холда.\n\n5️⃣ Полезные команды:\n🔸 /hold – показывает ваш холд (только для номеров с холдом от 54 мин).\n🔸 /del – удалить номер из очереди (формат: /del номер).\n🔸 /menu – обновить меню.\n\n6️⃣ Как повысить прайс? 🚀\nВ нашем боте можно повысить прайс с помощью подписки! Цены низкие, а бонусы сочные! 😍\n\nДоступные подписки:\n\nElite Access (+6,4$) 💵 Цена: 2 USDT\n\nGold Tier (+7$) 💰 Цена: 2,3 USDT\n\nPrime Plus (+9$) 🚀 Цена: 3 USDT\n\nVIP Nexus (+15$) 🔥 Цена: 4 USDT\n\nВсе подписки действуют 1 месяц (потом можно купить снова).</blockquote>"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['rules'], caption="Правила"), reply_markup=markup)
    bot.send_message(call.message.chat.id, rules_text, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data == "create_check_menu")
def create_check_menu(call):
    clear_pending_step(call.message.chat.id)
    caption = "🧾 Здесь вы можете создать чек для мгновенной отправки валюты любому пользователю."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Создать чек ➕", callback_data="create_check"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['profile'], caption=caption), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "create_check")
def create_check(call):
    clear_pending_step(call.message.chat.id)
    user = get_user(call.from_user.id)
    if user['card_status'] != 'active':
        bot.answer_callback_query(call.id, "Ваша карта не активна", show_alert=True)
        return
    balance = user['card_balance']
    caption = f"💰 Пришлите сумму чека.\nВаш баланс: {balance}$"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Мин. ·1$·", callback_data="check_amount_1"), types.InlineKeyboardButton(f"Макс. ·{balance}$·", callback_data=f"check_amount_{balance}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="create_check_menu"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    register_next_step(call.message.chat.id, process_check_amount, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("check_amount_"))
def check_amount(call):
    amount_str = call.data.split("_")[2]
    amount = float(amount_str)
    process_create_check(call.from_user.id, amount, call.message.message_id)

def process_check_amount(message, message_id):
    try:
        amount = float(message.text)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Неверный формат суммы. Попробуйте снова.")
        create_check(_SimpleNS(data="create_check", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user))
        return
    process_create_check(message.chat.id, amount, message_id)

def process_create_check(user_id, amount, message_id):
    user = get_user(user_id)
    balance = user['card_balance']
    if amount < 1 or amount > balance:
        bot.send_message(user_id, "❌ Сумма должна быть от 1$ до вашего баланса.")
        create_check(_SimpleNS(data="create_check", message=_SimpleNS(chat=_SimpleNS(id=user_id), message_id=message_id), from_user=_SimpleNS(id=user_id)))
        return
    unique_code = str(uuid.uuid4())
    cursor.execute("INSERT INTO checks (creator_id, amount, unique_code) VALUES (?, ?, ?)", (user_id, amount, unique_code))
    conn.commit()
    check_id = cursor.lastrowid
    update_user(user_id, card_balance=balance - amount)
    cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (user_id, -amount, datetime.now(tz), 'check_create'))
    conn.commit()
    show_check_options(user_id, check_id, message_id)

def show_check_options(chat_id, check_id, edit_id=None):
    clear_pending_step(chat_id)
    cursor.execute("SELECT * FROM checks WHERE id = ?", (check_id,))
    check = cursor.fetchone()
    if not check:
        bot.send_message(chat_id, "❌ Чек не найден.")
        return
    columns = [desc[0] for desc in cursor.description]
    check_dict = dict(zip(columns, check))
    amount = check_dict['amount']
    unique_code = check_dict['unique_code']
    link = f"https://t.me/{bot.get_me().username}?start=check_{unique_code}"
    description = check_dict['description'] or "Отсутствует"
    password = "Да" if check_dict['password'] else "Нет"
    image = "Да" if check_dict['image_file_id'] else "Нет"
    subs = json.loads(check_dict['require_subs'] or "[]")
    subs_status = "Вкл" if subs else "Выкл"
    premium_status = "Вкл" if check_dict['require_premium'] else "Выкл"
    caption = f"🧾 Мой чек\n💰 Сумма чека: {amount}$\n🔗 Ссылка на активацию чека: {link}"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("📝 Добавить описание", callback_data=f"add_desc_{check_id}"))
    markup.add(types.InlineKeyboardButton("🔑 Добавить пароль", callback_data=f"add_pass_{check_id}"))
    markup.add(types.InlineKeyboardButton("🖼️ Добавить картинку", callback_data=f"add_image_{check_id}"))
    markup.add(types.InlineKeyboardButton(f"📢 Проверка подписки: {subs_status}", callback_data=f"toggle_subs_{check_id}"))
    markup.add(types.InlineKeyboardButton(f"⭐ Только для Telegram Premium: {premium_status}", callback_data=f"toggle_premium_{check_id}"))
    markup.add(types.InlineKeyboardButton("📤 Поделиться чеком", callback_data=f"share_check_{check_id}"))
    markup.add(types.InlineKeyboardButton("🔲 QR-код", callback_data=f"qr_check_{check_id}"))
    markup.add(types.InlineKeyboardButton("🗑️ Удалить чек", callback_data=f"delete_check_{check_id}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="create_check_menu"))
    if edit_id:
        bot.edit_message_caption(caption, chat_id, edit_id, reply_markup=markup)
    else:
        bot.send_message(chat_id, caption, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("add_desc_"))
def add_desc(call):
    clear_pending_step(call.message.chat.id)
    check_id = int(call.data.split("_")[2])
    caption = "📝 Пришлите описание для чека (без ограничения символов)."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data=f"show_check_{check_id}"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    register_next_step(call.message.chat.id, process_add_desc, check_id, call.message.message_id)

def process_add_desc(message, check_id, message_id):
    description = message.text
    cursor.execute("UPDATE checks SET description = ? WHERE id = ?", (description, check_id))
    conn.commit()
    bot.send_message(message.chat.id, "✅ Описание добавлено.")
    show_check_options(message.chat.id, check_id, message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("add_pass_"))
def add_pass(call):
    clear_pending_step(call.message.chat.id)
    check_id = int(call.data.split("_")[2])
    caption = "🔑 Добро пожаловать в раздел ·Добавление пароля·.\nЗдесь вы можете добавить пароль на ваш чек перед активацией.\n·Пришлите ваш пароль в чат бота, пароль можно делать как цифрами, так и латинскими буквами.\n·Ограничение по символам нет."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data=f"show_check_{check_id}"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    register_next_step(call.message.chat.id, process_add_pass, check_id, call.message.message_id)

def process_add_pass(message, check_id, message_id):
    password = message.text
    caption = "✅ Молодец, пароль установлен.\nНо надо подтвердить установку пароля.\n·Нажми на кнопку ниже, чтобы установить пароль."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Подтвердить ✅", callback_data=f"confirm_pass_{check_id}_{password}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data=f"show_check_{check_id}"))
    bot.send_message(message.chat.id, caption, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_pass_"))
def confirm_pass(call):
    parts = call.data.split("_")
    check_id = int(parts[2])
    password = "_".join(parts[3:])  # if password has _
    cursor.execute("UPDATE checks SET password = ? WHERE id = ?", (password, check_id))
    conn.commit()
    bot.answer_callback_query(call.id, "✅ Пароль установлен.")
    show_check_options(call.message.chat.id, check_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("add_image_"))
def add_image(call):
    clear_pending_step(call.message.chat.id)
    check_id = int(call.data.split("_")[2])
    caption = "🖼️ Добро пожаловать в раздел ·Добавление картинки·.\nЗдесь вы можете добавить фото на ваш чек.\n·Пришлите ваше фото в разрешении 500 х 500."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data=f"show_check_{check_id}"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    register_next_step(call.message.chat.id, process_add_image, check_id, call.message.message_id)

def process_add_image(message, check_id, message_id):
    if not message.photo:
        bot.send_message(message.chat.id, "❌ Пожалуйста, пришлите фото.")
        add_image(_SimpleNS(data=f"add_image_{check_id}", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user))
        return
    file_id = message.photo[-1].file_id
    cursor.execute("UPDATE checks SET image_file_id = ? WHERE id = ?", (file_id, check_id))
    conn.commit()
    bot.send_message(message.chat.id, "✅ Картинка добавлена.")
    show_check_options(message.chat.id, check_id, message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle_subs_"))
def toggle_subs(call):
    clear_pending_step(call.message.chat.id)
    check_id = int(call.data.split("_")[2])
    caption = "📢 Добро пожаловать в раздел ·Проверка подписок·.\nЗдесь вы можете добавить проверку подписки на ваши каналы перед активацией чека.\nНо для начала вам надо будет добавить нашего \"@NFZ_WhatsApp_bot\" бота в свой канал в качестве администратора.\n·После того как добавите, нажмите на кнопку \"Я добавил\"."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Я добавил ✅", callback_data=f"added_bot_subs_{check_id}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data=f"show_check_{check_id}"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("added_bot_subs_"))
def added_bot_subs(call):
    check_id = int(call.data.split("_")[3])
    caption = "✅ Молодец, ты добавил бота в свой канал и сделал его администратором.\nТеперь введи название кнопки и ссылку на твой канал или группу.\nВ таком формате: ·Name https://t.me/NafuzorTime\nВводи название и ссылку через пробел."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data=f"toggle_subs_{check_id}"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    register_next_step(call.message.chat.id, process_add_sub, check_id, call.message.message_id)

def process_add_sub(message, check_id, message_id):
    text = message.text.strip()
    parts = text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "❌ Неверный формат. Попробуйте снова.")
        added_bot_subs(_SimpleNS(data=f"added_bot_subs_{check_id}", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user))
        return
    name = " ".join(parts[:-1])
    url = parts[-1]
    if not url.startswith("https://t.me/"):
        bot.send_message(message.chat.id, "❌ Неверная ссылка.")
        added_bot_subs(_SimpleNS(data=f"added_bot_subs_{check_id}", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user))
        return
    channel = url.split("/")[-1]
    if not channel:
        bot.send_message(message.chat.id, "❌ Неверная ссылка.")
        added_bot_subs(_SimpleNS(data=f"added_bot_subs_{check_id}", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user))
        return
    cursor.execute("SELECT require_subs FROM checks WHERE id = ?", (check_id,))
    subs_json = cursor.fetchone()[0] or "[]"
    subs = json.loads(subs_json)
    subs.append({"name": name, "url": url, "channel": channel})
    cursor.execute("UPDATE checks SET require_subs = ? WHERE id = ?", (json.dumps(subs), check_id))
    conn.commit()
    bot.send_message(message.chat.id, "✅ Подписка добавлена.")
    show_check_options(message.chat.id, check_id, message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle_premium_"))
def toggle_premium(call):
    check_id = int(call.data.split("_")[2])
    cursor.execute("SELECT require_premium FROM checks WHERE id = ?", (check_id,))
    current = cursor.fetchone()[0]
    new = 1 if current == 0 else 0
    cursor.execute("UPDATE checks SET require_premium = ? WHERE id = ?", (new, check_id))
    conn.commit()
    bot.answer_callback_query(call.id, f"⭐ Функция только для Premium {'включена' if new else 'выключена'}.")
    show_check_options(call.message.chat.id, check_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("share_check_"))
def share_check(call):
    check_id = int(call.data.split("_")[2])
    cursor.execute("SELECT amount, unique_code FROM checks WHERE id = ?", (check_id,))
    row = cursor.fetchone()
    if not row:
        bot.answer_callback_query(call.id, "❌ Чек не найден.")
        return
    amount, unique_code = row
    link = f"https://t.me/{bot.get_me().username}?start=check_{unique_code}"
    caption = f"🦋 Чек на {amount} USDT 🪙"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Получить ✅", url=link))
    bot.send_message(call.message.chat.id, caption, reply_markup=markup)
    bot.answer_callback_query(call.id, "📤 Чек для分享. Вы можете переслать это сообщение.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("qr_check_"))
def qr_check(call):
    check_id = int(call.data.split("_")[2])
    cursor.execute("SELECT unique_code FROM checks WHERE id = ?", (check_id,))
    unique_code = cursor.fetchone()[0]
    link = f"https://t.me/{bot.get_me().username}?start=check_{unique_code}"
    qr_url = f"https://quickchart.io/qr?text={requests.utils.quote(link)}&size=200"
    bot.send_photo(call.message.chat.id, qr_url)
    bot.answer_callback_query(call.id, "🔲 QR-код для чека.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_check_"))
def delete_check(call):
    check_id = int(call.data.split("_")[2])
    cursor.execute("SELECT creator_id, amount, activated_at FROM checks WHERE id = ?", (check_id,))
    row = cursor.fetchone()
    if not row:
        bot.answer_callback_query(call.id, "❌ Чек не найден.")
        return
    creator_id, amount, activated_at = row
    if activated_at:
        bot.answer_callback_query(call.id, "❌ Чек уже активирован, нельзя удалить.")
        return
    user = get_user(creator_id)
    update_user(creator_id, card_balance=user['card_balance'] + amount)
    cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (creator_id, amount, datetime.now(tz), 'check_delete'))
    cursor.execute("DELETE FROM checks WHERE id = ?", (check_id,))
    conn.commit()
    bot.answer_callback_query(call.id, "🗑️ Чек удален, средства возвращены.")
    create_check_menu(call)

@bot.callback_query_handler(func=lambda call: call.data.startswith("show_check_"))
def show_check(call):
    check_id = int(call.data.split("_")[2])
    show_check_options(call.message.chat.id, check_id, call.message.message_id)

def handle_check_activation(message, unique_code):
    cursor.execute("SELECT * FROM checks WHERE unique_code = ?", (unique_code,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "❌ Чек не найден.")
        return
    columns = [desc[0] for desc in cursor.description]
    check = dict(zip(columns, row))
    if check['activated_at']:
        bot.send_message(message.chat.id, "❌ Этот чек уже активирован.")
        return
    user_id = message.from_user.id
    creator_id = check['creator_id']
    amount = check['amount']
    description = check['description'] or ""
    password = check['password']
    image_file_id = check['image_file_id']
    require_subs = json.loads(check['require_subs'] or "[]")
    require_premium = check['require_premium']
    is_premium = message.from_user.is_premium if hasattr(message.from_user, 'is_premium') else False
    if require_premium and not is_premium and user_id != creator_id:
        bot.send_message(message.chat.id, "❌ Этот чек только для пользователей с Telegram Premium.")
        return
    if image_file_id:
        bot.send_photo(message.chat.id, image_file_id)
    caption = f"🧾 Активация чека на сумму {amount}$\n📝 Описание: {description}"
    markup = types.InlineKeyboardMarkup()
    if require_subs:
        caption += "\n📢 Требуется подписка на каналы."
        for sub in require_subs:
            markup.add(types.InlineKeyboardButton(sub['name'], url=sub['url']))
        markup.add(types.InlineKeyboardButton("Проверить подписку 🔍", callback_data=f"check_subs_activate_{check['id']}"))
        bot.send_message(message.chat.id, caption, reply_markup=markup)
        return
    if password:
        caption += "\n🔑 Требуется пароль."
        bot.send_message(message.chat.id, caption)
        register_next_step(message.chat.id, process_activate_password, check['id'])
        return
    activate_check(user_id, check['id'])

@bot.callback_query_handler(func=lambda call: call.data.startswith("check_subs_activate_"))
def check_subs_activate(call):
    check_id = int(call.data.split("_")[3])
    cursor.execute("SELECT require_subs, password FROM checks WHERE id = ?", (check_id,))
    row = cursor.fetchone()
    require_subs = json.loads(row[0] or "[]")
    password = row[1]
    user_id = call.from_user.id
    all_subbed = True
    for sub in require_subs:
        try:
            member = bot.get_chat_member(f"@{sub['channel']}", user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                all_subbed = False
                break
        except:
            all_subbed = False
            break
    if not all_subbed:
        bot.answer_callback_query(call.id, "❌ Вы не подписаны на все каналы. Подпишитесь и попробуйте снова.")
        return
    if password:
        bot.send_message(call.message.chat.id, "🔑 Введите пароль для активации.")
        register_next_step(call.message.chat.id, process_activate_password, check_id)
        return
    activate_check(user_id, check_id)
    bot.answer_callback_query(call.id, "✅ Подписки проверены.")

def process_activate_password(message, check_id):
    password = message.text
    cursor.execute("SELECT password FROM checks WHERE id = ?", (check_id,))
    correct = cursor.fetchone()[0]
    if password != correct:
        bot.send_message(message.chat.id, "❌ Неверный пароль. Попробуйте снова.")
        register_next_step(message.chat.id, process_activate_password, check_id)
        return
    activate_check(message.chat.id, check_id)

def activate_check(user_id, check_id):
    cursor.execute("SELECT creator_id, amount, activated_at FROM checks WHERE id = ?", (check_id,))
    row = cursor.fetchone()
    if row[2]:  # activated_at
        bot.send_message(user_id, "❌ Чек уже активирован.")
        return
    creator_id, amount = row[0], row[1]
    user = get_user(user_id)
    update_user(user_id, card_balance=user['card_balance'] + amount)
    cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (user_id, amount, datetime.now(tz), 'check_activate'))
    cursor.execute("UPDATE checks SET activated_by = ?, activated_at = ? WHERE id = ?", (user_id, datetime.now(tz), check_id))
    conn.commit()
    activator_username = user['username']
    creator_username = get_user(creator_id)['username']
    bot.send_message(user_id, f"✅ Вы активировали чек от @{creator_username} и получили {amount} USDT 🪙.")
    bot.send_message(creator_id, f"✅ @{activator_username} активировал ваш чек и получил {amount} USDT 🪙.")

@bot.callback_query_handler(func=lambda call: call.data == "buy_sub")
def buy_sub(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    caption = "Купить подписку"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("🌟Telegram stars🌟", callback_data="pay_stars"), types.InlineKeyboardButton("🌐CryptoBot🌐", callback_data="pay_crypto"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['buy_sub'], caption=caption), reply_markup=markup)

# Определяем SUBSCRIPTIONS если не определено в config
if not hasattr(config, 'SUBSCRIPTIONS'):
    config.SUBSCRIPTIONS = {
        'Elite Access': {
            'price_increase_hour': 6.4,
            'price_increase_30min': 3.2,
            'price_crypto': 2,
            'price_stars': 30
        },
        'Gold Tier': {
            'price_increase_hour': 7,
            'price_increase_30min': 3.5,
            'price_crypto': 2.3,
            'price_stars': 55
        },
        'Prime Plus': {
            'price_increase_hour': 9,
            'price_increase_30min': 4.5,
            'price_crypto': 3,
            'price_stars': 88
        },
        'VIP Nexus': {
            'price_increase_hour': 15,
            'price_increase_30min': 7.5,
            'price_crypto': 4,
            'price_stars': 299
        }
    }

@bot.callback_query_handler(func=lambda call: call.data == "pay_stars")
def pay_stars(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    caption = "🌒Выбери подписку которую хочешь купить:\n— Способ: 🌟 Telegram stars 🌟"
    markup = types.InlineKeyboardMarkup(row_width=1)
    for sub in config.SUBSCRIPTIONS:
        markup.add(types.InlineKeyboardButton(f"🌑{sub}🌕", callback_data=f"sub_stars_{sub}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="buy_sub"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "pay_crypto")
def pay_crypto(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    caption = "🌒Выбери подписку которую хочешь купить:\n— Способ: 🌐CryptoBot🌐"
    markup = types.InlineKeyboardMarkup(row_width=1)
    for sub in config.SUBSCRIPTIONS:
        markup.add(types.InlineKeyboardButton(f"🌑{sub}🌕", callback_data=f"sub_crypto_{sub}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="buy_sub"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("sub_stars_"))
def sub_stars(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    sub_type = call.data.split("_")[2]
    sub = config.SUBSCRIPTIONS.get(sub_type, {})
    price = sub.get('price_stars', 0)
    if price == 0:
        bot.answer_callback_query(call.id, "Ошибка: подписка не найдена", show_alert=True)
        return
    payload = f"sub_{sub_type}_{call.from_user.id}_{random.randint(1, 1000000)}"
    cursor.execute("INSERT INTO payments (user_id, sub_type, amount, payload) VALUES (?, ?, ?, ?)",
                   (call.from_user.id, sub_type, price, payload))
    conn.commit()
    payment_id = cursor.lastrowid
    caption = f"💸 Оплатите счёт\n— Способ: 🌟 Telegram stars 🌟\n— Сумма: {price} Stars"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Оплатить", callback_data=f"pay_stars_inv_{payment_id}"))
    markup.add(types.InlineKeyboardButton("Проверить", callback_data=f"check_stars_{payment_id}"))
    markup.add(types.InlineKeyboardButton("Отмена", callback_data="pay_stars"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_stars_inv_"))
def pay_stars_inv(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    payment_id = int(call.data.split("_")[3])
    cursor.execute("SELECT sub_type, payload, amount FROM payments WHERE id = ?", (payment_id,))
    row = cursor.fetchone()
    if row:
        sub_type, payload, amount = row
        prices_list = [types.LabeledPrice(label=f"Оплата подписки {sub_type}", amount=int(amount))]
        bot.send_invoice(
            call.message.chat.id,
            title=f"Подписка {sub_type}",
            description="На 1 месяц",
           
            invoice_payload=payload,
            provider_token='',
            currency='XTR',
            prices=prices_list
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("check_stars_"))
def check_stars(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    payment_id = int(call.data.split("_")[2])
    cursor.execute("SELECT status FROM payments WHERE id = ?", (payment_id,))
    row = cursor.fetchone()
    if row and row[0] == 'paid':
        bot.answer_callback_query(call.id, "Оплата подтверждена! Подписка активирована.")
    else:
        bot.answer_callback_query(call.id, "Оплата не подтверждена.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("sub_crypto_"))
def sub_crypto(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    try:
        sub_type = call.data.split("_")[2]
        sub = config.SUBSCRIPTIONS.get(sub_type, {})
        price = sub.get('price_crypto', 0)
        
        if price == 0:
            bot.answer_callback_query(call.id, "Ошибка: подписка не найдена", show_alert=True)
            return

        payload = f"sub_{sub_type}_{call.from_user.id}_{random.randint(1, 1000000)}"
        asset = 'USDT'
        description = f"Покупка подписки {sub_type} на 1 месяц"

        # Правильный формат запроса с заголовками
        headers = {
            'Crypto-Pay-API-Token': config.CRYPTO_TOKEN,
            'Content-Type': 'application/json'
        }
        
        data = {
            'asset': asset,
            'amount': str(price),
            'description': description,
            'payload': payload
        }

        response = requests.post('https://pay.crypt.bot/api/createInvoice', 
                               headers=headers,
                               json=data)

        if response.status_code == 200:
            data = response.json()
            if data.get('ok'):
                invoice = data['result']
                invoice_id = invoice['invoice_id']
                pay_url = invoice['pay_url']

                # Сохраняем в БД
                cursor.execute("INSERT INTO payments (user_id, sub_type, amount, invoice_id) VALUES (?, ?, ?, ?)",
                               (call.from_user.id, sub_type, price, invoice_id))
                conn.commit()
                payment_id = cursor.lastrowid

                caption = f"💸 Оплатите счёт\n— Способ: 🌐CryptoBot🌐\n— Сумма: {price} USDT"
                markup = types.InlineKeyboardMarkup(row_width=2)
                markup.add(types.InlineKeyboardButton("Оплатить", url=pay_url))
                markup.add(types.InlineKeyboardButton("Проверить", callback_data=f"check_crypto_{payment_id}"))
                markup.add(types.InlineKeyboardButton("Отмена", callback_data="pay_crypto"))
                bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
            else:
                error_msg = data.get('error', {}).get('name', 'Unknown error')
                bot.answer_callback_query(call.id, f"Ошибка CryptoBot: {error_msg}", show_alert=True)
                print(f"CryptoBot createInvoice error: {data}")
        else:
            bot.answer_callback_query(call.id, f"Ошибка API: {response.status_code}", show_alert=True)
            print(f"CryptoBot createInvoice HTTP error: {response.status_code} - {response.text}")
            
    except Exception as e:
        bot.answer_callback_query(call.id, f"Ошибка: {str(e)}", show_alert=True)
        print(f"Exception in sub_crypto: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("check_crypto_"))
def check_crypto(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    try:
        payment_id = int(call.data.split("_")[2])
        cursor.execute("SELECT invoice_id FROM payments WHERE id = ?", (payment_id,))
        row = cursor.fetchone()
        if not row:
            bot.answer_callback_query(call.id, "Инвойс не найден", show_alert=True)
            return
        invoice_id = row[0]

        headers = {
            'Crypto-Pay-API-Token': config.CRYPTO_TOKEN,
            'Content-Type': 'application/json'
        }

        response = requests.get(f'https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}', 
                              headers=headers)

        if response.status_code == 200:
            data = response.json()
            if data.get('ok'):
                invoices = data['result']['items']
                if invoices:
                    status = invoices[0]['status']
                    if status == 'paid':
                        cursor.execute("SELECT user_id, sub_type FROM payments WHERE id = ? AND status = 'pending'", (payment_id,))
                        row = cursor.fetchone()
                        if row:
                            user_id, sub_type = row
                            end = datetime.now(tz) + timedelta(days=30)
                            update_user(user_id, subscription_type=sub_type, subscription_end=end)
                            cursor.execute("UPDATE payments SET status = 'paid' WHERE id = ?", (payment_id,))
                            conn.commit()
                            bot.answer_callback_query(call.id, "Оплата подтверждена! Подписка активирована.")
                            bot.send_message(call.message.chat.id, f"Подписка {sub_type} активирована на 30 дней.")
                        else:
                            bot.answer_callback_query(call.id, "Оплата уже обработана", show_alert=True)
                    else:
                        bot.answer_callback_query(call.id, f"Статус оплаты: {status}. Попробуйте позже.", show_alert=True)
                else:
                    bot.answer_callback_query(call.id, "Инвойс не найден", show_alert=True)
            else:
                error_msg = data.get('error', {}).get('name', 'Unknown error')
                bot.answer_callback_query(call.id, f"Ошибка API: {error_msg}", show_alert=True)
                print(f"CryptoBot getInvoices error: {data}")
        else:
            bot.answer_callback_query(call.id, f"Ошибка соединения: {response.status_code}", show_alert=True)
            print(f"CryptoBot getInvoices HTTP error: {response.status_code} - {response.text}")
    except Exception as e:
        bot.answer_callback_query(call.id, f"Ошибка: {str(e)}", show_alert=True)
        print(f"Exception in check_crypto: {e}")

@bot.pre_checkout_query_handler(func=lambda query: True)
def pre_checkout(query):
    bot.answer_pre_checkout_query(query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def successful_payment(message):
    clear_pending_step(message.chat.id)  # Очищаем pending
    payload = message.successful_payment.invoice_payload
    if payload.startswith('sub_'):
        parts = payload.split('_')
        sub_type = parts[1]
        user_id = int(parts[2])
        if user_id == message.from_user.id:
            end = datetime.now(tz) + timedelta(days=30)
            update_user(user_id, subscription_type=sub_type, subscription_end=end)
            cursor.execute("UPDATE payments SET status = 'paid', transaction_id = ? WHERE payload = ?", (message.successful_payment.telegram_payment_charge_id, payload))
            conn.commit()
            bot.send_message(message.chat.id, f"Подписка {sub_type} активирована!")
    elif payload.startswith('deposit_'):
        cursor.execute("SELECT id, user_id, amount FROM payments WHERE payload = ? AND sub_type = 'deposit' AND status = 'pending'", (payload,))
        row = cursor.fetchone()
        if row:
            payment_id, user_id, amount = row
            if user_id == message.from_user.id:
                cursor.execute("UPDATE payments SET status = 'paid', transaction_id = ? WHERE id = ?", (message.successful_payment.telegram_payment_charge_id, payment_id))
                conn.commit()
                deposit_amount = amount / 2  # 2 stars = 1$
                user = get_user(user_id)
                update_user(user_id, card_balance=user['card_balance'] + deposit_amount)
                cursor.execute("INSERT INTO deposit_history (user_id, amount, created_at, request_id) VALUES (?, ?, ?, ?)",
                               (user_id, deposit_amount, datetime.now(tz), payment_id))
                conn.commit()
                bot.send_message(message.chat.id, f"Счет пополнен на {deposit_amount}$!")
                # Возвращаем в карту
                display_card(message.chat.id, message.message_id)



# Assuming the truncated part includes the payment handlers, etc.

@bot.callback_query_handler(func=lambda call: call.data == "referral")
def show_referral(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    user = get_user(call.message.chat.id)
    referrals = user['referrals_count']
    balance = user['balance']
    ref_link = f"https://t.me/{bot.get_me().username}?start={user['referral_code']}"
    caption = f"💎 Реферальная система\n\n<blockquote>📔 Наша реферальная система позволит вам заработать крупную сумму без вложений. \nДостаточно давать свою ссылку друзьям — и от каждой покупки вашего реферала вы будете получать 0.5$ на свой баланс.</blockquote>\n\n🔗 Ссылка: {ref_link}\n\n💰 Заработано: {balance}$\n\n👤 Рефералов: {referrals}"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Создать заявку 💸", callback_data="withdraw"), types.InlineKeyboardButton("Мои заявки 📋", callback_data="requests_list"))
    markup.row(types.InlineKeyboardButton("История зачислений💾", callback_data="deposit_history"))
    markup.row(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['referral'], caption=caption, parse_mode='HTML'), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "deposit_history")
def show_deposit_history(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    user_id = call.from_user.id
    cursor.execute("SELECT id, amount, paid_at FROM withdraw_requests WHERE user_id = ? AND status = 'paid' ORDER BY paid_at DESC", (user_id,))
    requests = cursor.fetchall()
    caption = "История зачислений:"
    markup = types.InlineKeyboardMarkup(row_width=1)
    if requests:
        for req in requests:
            markup.add(types.InlineKeyboardButton(f"🖥️{req[1]}$", callback_data=f"view_deposit_{req[0]}"))
    else:
        caption += "\n\nНет зачислений"
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="referral"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_deposit_"))
def view_deposit(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    req_id = int(call.data.split("_")[2])
    cursor.execute("SELECT amount, paid_at, id FROM withdraw_requests WHERE id = ? AND user_id = ?", (req_id, call.from_user.id))
    req = cursor.fetchone()
    if not req:
        bot.answer_callback_query(call.id, "Заявка не найдена", show_alert=True)
        return
    dt = req[1].astimezone(tz).strftime('%Y-%m-%d %H:%M:%S') if req[1] else 'N/A'
    caption = f"🗒️История пополнения: \n\n💲Сумма: {req[0]}\n🗓️Дата пополнения: {dt}\n📟Номер заявки: {req[2]}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="deposit_history"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "requests_list")
def show_my_requests(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    user_id = call.from_user.id
    cursor.execute("SELECT id, amount, status, created_at FROM withdraw_requests WHERE user_id = ? AND status = 'pending' ORDER BY id DESC", (user_id,))
    requests = cursor.fetchall()
    caption = "Мои заявки:"
    markup = types.InlineKeyboardMarkup(row_width=1)
    if requests:
        for req in requests:
            markup.add(types.InlineKeyboardButton(f"🖥️ Заявка №{req[0]:06d}", callback_data=f"view_request_{req[0]}"))
    else:
        caption += "\n\nНет заявок"
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="referral"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_request_"))
def view_request(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    req_id = int(call.data.split("_")[2])
    cursor.execute("SELECT * FROM withdraw_requests WHERE id = ?", (req_id,))
    req = cursor.fetchone()
    if not req or req[1] != call.from_user.id:
        bot.answer_callback_query(call.id, "Заявка не найдена", show_alert=True)
        return
    user = get_user(req[1])
    dt = req[4].astimezone(tz).strftime('%Y-%m-%d %H:%M:%S') if req[4] else 'N/A'
    caption = f"💾Заявка №{req[0]:06d}\n\n🗒️Блан заполнения:\n\n🔹Юзернейм: @{user['username']}\n🔹Сумма выплаты: ${req[2]}\n🔹Дата создание заявки: {dt}"
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("Изменить сумму", callback_data=f"edit_amount_{req[0]}"))
    markup.add(types.InlineKeyboardButton("Закрыть заявку", callback_data=f"close_request_{req[0]}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="requests_list"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_amount_"))
def edit_amount(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending перед новым input
    req_id = int(call.data.split("_")[2])
    caption = "Введите новую сумму"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data=f"view_request_{req_id}"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    register_next_step(call.message.chat.id, process_edit_amount, call.message.message_id, req_id)

def process_edit_amount(message, message_id, req_id):
    try:
        new_amount = float(message.text)
    except ValueError:
        bot.send_message(message.chat.id, "Неверный формат")
        fake_call = _SimpleNS(data=f"view_request_{req_id}", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user)
        view_request(fake_call)
        return
    if new_amount < 50:
        bot.send_message(message.chat.id, "Сумма ниже минимума")
        fake_call = _SimpleNS(data=f"view_request_{req_id}", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user)
        view_request(fake_call)
        return
    cursor.execute("SELECT amount, user_id FROM withdraw_requests WHERE id = ?", (req_id,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Заявка не найдена")
        return
    old_amount, user_id = row
    user = get_user(user_id)
    diff = new_amount - old_amount
    if diff > 0:
        if user['balance'] < diff:
            bot.send_message(message.chat.id, "Недостаточно средств")
            fake_call = _SimpleNS(data=f"view_request_{req_id}", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user)
            view_request(fake_call)
            return
        update_user(user_id, balance=user['balance'] - diff)
    elif diff < 0:
        update_user(user_id, balance=user['balance'] - diff)  # - negative = +
    cursor.execute("UPDATE withdraw_requests SET amount = ? WHERE id = ?", (new_amount, req_id))
    conn.commit()
    bot.send_message(message.chat.id, "Сумма изменена")
    fake_call = _SimpleNS(data=f"view_request_{req_id}", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user)
    view_request(fake_call)

@bot.callback_query_handler(func=lambda call: call.data.startswith("close_request_"))
def close_request(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    req_id = int(call.data.split("_")[2])
    cursor.execute("SELECT amount, user_id, status FROM withdraw_requests WHERE id = ?", (req_id,))
    row = cursor.fetchone()
    if not row or row[2] != 'pending':
        bot.answer_callback_query(call.id, "Заявка не может быть закрыта", show_alert=True)
        return
    amount, user_id = row[0], row[1]
    user = get_user(user_id)
    update_user(user_id, balance=user['balance'] + amount)
    cursor.execute("UPDATE withdraw_requests SET status = 'closed' WHERE id = ?", (req_id,))
    conn.commit()
    bot.answer_callback_query(call.id, "Заявка закрыта")
    show_my_requests(call)

@bot.callback_query_handler(func=lambda call: call.data == "withdraw")
def withdraw(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending перед новым input
    user = get_user(call.message.chat.id)
    if user['balance'] < 50:
        bot.answer_callback_query(call.id, "Минимальный вывод $50", show_alert=True)
        return
    caption = "Укажите сумму вывода"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="referral"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['referral'], caption=caption), reply_markup=markup)
    register_next_step(call.message.chat.id, process_withdraw, call.message.message_id)

def process_withdraw(message, message_id):
    try:
        amount = float(message.text)
    except ValueError:
        bot.send_message(message.chat.id, "Неверный формат")
        fake_call = _SimpleNS(data="referral", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user)
        show_referral(fake_call)
        return
    user = get_user(message.chat.id)
    if amount > user['balance'] or amount < 50:
        bot.send_message(message.chat.id, "Недостаточно средств или ниже минимума")
        fake_call = _SimpleNS(data="referral", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user)
        show_referral(fake_call)
        return
    cursor.execute("INSERT INTO withdraw_requests (user_id, amount, created_at) VALUES (?, ?, ?)", (message.chat.id, amount, datetime.now(tz)))
    conn.commit()
    update_user(message.chat.id, balance=user['balance'] - amount)
    bot.send_message(message.chat.id, "Заявка создана")
    fake_call = _SimpleNS(data="referral", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user)
    show_referral(fake_call)



@bot.callback_query_handler(func=lambda call: call.data == "card")
def show_card(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending перед возможным input
    user = get_user(call.message.chat.id)
    if user['card_status'] == 'blocked':
        if user['block_reason'] == 'admin':
            caption = "Карта заблокирована администратором"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
            bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
            return
        elif user['block_reason'] == 'user':
            if user['card_activation_date'] and (datetime.now(tz) - user['card_activation_date']) >= timedelta(days=30):
                update_user(call.message.chat.id, card_status='inactive', block_reason=None)
                user = get_user(call.message.chat.id)  # Reload user
            else:
                remaining = timedelta(days=30) - (datetime.now(tz) - user['card_activation_date'])
                caption = f"Карта заблокирована на 30 дней. Осталось: {remaining.days} дней"
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
                bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
                return

    if user['card_status'] == 'inactive':
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Активировать 🔓", callback_data="activate_card"))
        markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
        bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['card'], caption="Карта не активирована"), reply_markup=markup)
        return

    # active
    caption = "Введите пароль от карты"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
    bot.edit_message_media(chat_id=call.message.chat.id, message_id=call.message.message_id, media=types.InputMediaPhoto(photos.PHOTOS['card'], caption=caption), reply_markup=markup)
    register_next_step(call.message.chat.id, check_card_password, call.message.message_id)

def check_card_password(message, message_id):
    user = get_user(message.chat.id)
    if message.text != user['card_password']:
        bot.send_message(message.chat.id, "Неверный пароль")
        show_profile(_SimpleNS(message=message, from_user=message.from_user, data="profile"))
        return
    display_card(message.chat.id, message_id)

def display_card(chat_id, edit_id):
    clear_pending_step(chat_id)  # Очищаем pending
    user = get_user(chat_id)
    card_num = user['card_number']
    cvv = user['cvv']
    balance = user['card_balance']
    status = 'активна' if user['card_status'] == 'active' else 'заблокирована'
    if not user.get('api_token'):
        api_token = generate_api_token(chat_id)
        update_user(chat_id, api_token=api_token)
    caption = f"💳номер карты: {card_num}\n⚙️CVV: {cvv}\n💰баланс: {balance}\n💾информация о карте: {status}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Настройки ⚙️", callback_data="card_settings"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="profile"))
    bot.edit_message_media(chat_id=chat_id, message_id=edit_id, media=types.InputMediaPhoto(photos.PHOTOS['card'], caption=caption), reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "back_card_no_pass")
def back_card_no_pass(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    display_card(call.message.chat.id, call.message.message_id)

# ... (остальной код без изменений)

@bot.callback_query_handler(func=lambda call: call.data == "card_settings")
def card_settings(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    caption = "Настройки карты"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("API token 🔑", callback_data="api_card"),
               types.InlineKeyboardButton("История 📜", callback_data="card_history_user"))
    markup.row(types.InlineKeyboardButton("Пополнить счёт 💰", callback_data="deposit_card"))
    markup.add(types.InlineKeyboardButton("Перевести 💸", callback_data="transfer_money"),
               types.InlineKeyboardButton("Заблокировать карту 🛑", callback_data="block_card"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="back_card_no_pass"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "deposit_card")
def deposit_card(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    caption = "Добро пожаловать в пополнении счета:"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("⭐ Telegram Stars", callback_data="deposit_stars"),
               types.InlineKeyboardButton("🌐 Crypto Bot", callback_data="deposit_crypto"))
    markup.row(types.InlineKeyboardButton("📊 Курс", callback_data="deposit_rates"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="card_settings"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "deposit_rates")
def deposit_rates(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    caption = "»курс пополнения«\n\nзвёздами» 2🌟 = 1$ | мин.сумма пополнения 10$\n\nкрипта» 2$ = 2$ | мин.сумма пополнения 10$"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="deposit_card"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "deposit_stars")
def deposit_stars(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending перед новым input
    caption = "Введите сумму пополнения в $ (минимум 10$)"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="deposit_card"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    register_next_step(call.message.chat.id, process_deposit_stars, call.message.message_id)

def process_deposit_stars(message, message_id):
    try:
        deposit_amount = float(message.text)
        if deposit_amount < 10:
            bot.send_message(message.chat.id, "Минимальная сумма 10$")
            fake_call = _SimpleNS(data="deposit_stars", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user)
            deposit_stars(fake_call)
            return
        stars_amount = int(deposit_amount * 2)  # 2 stars = 1$
    except ValueError:
        bot.send_message(message.chat.id, "Неверный формат")
        fake_call = _SimpleNS(data="deposit_stars", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user)
        deposit_stars(fake_call)
        return
    payload = f"deposit_{message.chat.id}_{random.randint(1, 1000000)}"
    cursor.execute("INSERT INTO payments (user_id, sub_type, amount, payload) VALUES (?, ?, ?, ?)",
                   (message.chat.id, 'deposit', stars_amount, payload))
    conn.commit()
    payment_id = cursor.lastrowid
    created_at = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')
    caption = f"🏦 Способ оплаты: ⭐ Telegram Stars\n💰 Стоимость: {stars_amount} Stars\n📅 Создан: {created_at}\n⏰ Произведите оплату в течение 120 минут."
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Оплатить ✅", callback_data=f"pay_deposit_stars_{payment_id}"),
               types.InlineKeyboardButton("Проверить 🔍", callback_data=f"check_deposit_stars_{payment_id}"))
    markup.row(types.InlineKeyboardButton("Отмена ❌", callback_data="deposit_card"))
    bot.edit_message_caption(caption, message.chat.id, message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_deposit_stars_"))
def pay_deposit_stars(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    payment_id = int(call.data.split("_")[3])
    cursor.execute("SELECT payload, amount FROM payments WHERE id = ? AND sub_type = 'deposit'", (payment_id,))
    row = cursor.fetchone()
    if row:
        payload, amount = row
        prices_list = [types.LabeledPrice(label="Пополнение счета", amount=int(amount))]
        bot.send_invoice(
            call.message.chat.id,
            title="Пополнение счета",
            description="Через Telegram Stars",
            invoice_payload=payload,
            provider_token='',
            currency='XTR',
            prices=prices_list
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("check_deposit_stars_"))
def check_deposit_stars(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    payment_id = int(call.data.split("_")[3])
    cursor.execute("SELECT status FROM payments WHERE id = ? AND sub_type = 'deposit'", (payment_id,))
    row = cursor.fetchone()
    if row and row[0] == 'paid':
        bot.answer_callback_query(call.id, "Оплата подтверждена! Счет пополнен.")
        display_card(call.message.chat.id, call.message.message_id)
    else:
        bot.answer_callback_query(call.id, "Оплата не подтверждена.")

@bot.callback_query_handler(func=lambda call: call.data == "deposit_crypto")
def deposit_crypto(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending перед новым input
    caption = "Введите сумму пополнения в $ (минимум 10$)"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="deposit_card"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    register_next_step(call.message.chat.id, process_deposit_crypto, call.message.message_id)

def process_deposit_crypto(message, message_id):
    try:
        deposit_amount = float(message.text)
        if deposit_amount < 10:
            bot.send_message(message.chat.id, "Минимальная сумма 10$")
            fake_call = _SimpleNS(data="deposit_crypto", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user)
            deposit_crypto(fake_call)
            return
        usdt_amount = deposit_amount  # 1:1
    except ValueError:
        bot.send_message(message.chat.id, "Неверный формат")
        fake_call = _SimpleNS(data="deposit_crypto", message=_SimpleNS(chat=_SimpleNS(id=message.chat.id), message_id=message_id), from_user=message.from_user)
        deposit_crypto(fake_call)
        return
    
    payload = f"deposit_{message.chat.id}_{random.randint(1, 1000000)}"
    asset = 'USDT'
    description = "Пополнение счета через Crypto Bot"

    headers = {
        'Crypto-Pay-API-Token': config.CRYPTO_TOKEN,
        'Content-Type': 'application/json'
    }
    
    data = {
        'asset': asset,
        'amount': str(usdt_amount),
        'description': description,
        'payload': payload
    }

    response = requests.post('https://pay.crypt.bot/api/createInvoice', headers=headers, json=data)

    if response.status_code == 200:
        resp_data = response.json()
        if resp_data.get('ok'):
            invoice = resp_data['result']
            invoice_id = invoice['invoice_id']
            pay_url = invoice['pay_url']

            cursor.execute("INSERT INTO payments (user_id, sub_type, amount, invoice_id, payload) VALUES (?, ?, ?, ?, ?)",
                           (message.chat.id, 'deposit', usdt_amount, invoice_id, payload))
            conn.commit()
            payment_id = cursor.lastrowid

            created_at = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')
            caption = f"🏦 Способ оплаты: 🌐 Crypto Bot\n💰 Стоимость: {usdt_amount} USDT\n📅 Создан: {created_at}\n⏰ Произведите оплату в течение 120 минут."
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(types.InlineKeyboardButton("Оплатить ✅", url=pay_url),
                       types.InlineKeyboardButton("Проверить 🔍", callback_data=f"check_deposit_crypto_{payment_id}"))
            markup.row(types.InlineKeyboardButton("Отмена ❌", callback_data="deposit_card"))
            bot.edit_message_caption(caption, message.chat.id, message_id, reply_markup=markup)
        else:
            bot.answer_callback_query(call.id, "Ошибка создания инвойса", show_alert=True)
    else:
        bot.answer_callback_query(call.id, "Ошибка соединения", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("check_deposit_crypto_"))
def check_deposit_crypto(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending
    payment_id = int(call.data.split("_")[3])
    cursor.execute("SELECT invoice_id, status FROM payments WHERE id = ? AND sub_type = 'deposit'", (payment_id,))
    row = cursor.fetchone()
    if not row:
        bot.answer_callback_query(call.id, "Инвойс не найден", show_alert=True)
        return
    invoice_id, status = row
    if status == 'paid':
        bot.answer_callback_query(call.id, "Оплата уже подтверждена", show_alert=True)
        return

    headers = {
        'Crypto-Pay-API-Token': config.CRYPTO_TOKEN,
        'Content-Type': 'application/json'
    }

    response = requests.get(f'https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}', headers=headers)

    if response.status_code == 200:
        data = response.json()
        if data.get('ok'):
            invoices = data['result']['items']
            if invoices and invoices[0]['status'] == 'paid':
                cursor.execute("UPDATE payments SET status = 'paid' WHERE id = ?", (payment_id,))
                conn.commit()
                cursor.execute("SELECT user_id, amount FROM payments WHERE id = ?", (payment_id,))
                user_id, amount = cursor.fetchone()
                user = get_user(user_id)
                update_user(user_id, card_balance=user['card_balance'] + (amount / 1))  # 1:1 для crypto
                cursor.execute("INSERT INTO deposit_history (user_id, amount, created_at, request_id) VALUES (?, ?, ?, ?)",
                               (user_id, amount, datetime.now(tz), payment_id))
                conn.commit()
                bot.answer_callback_query(call.id, "Оплата подтверждена! Счет пополнен.")
                display_card(call.message.chat.id, call.message.message_id)
            else:
                bot.answer_callback_query(call.id, "Оплата не подтверждена.")
        else:
            bot.answer_callback_query(call.id, "Ошибка API", show_alert=True)
    else:
        bot.answer_callback_query(call.id, "Ошибка соединения", show_alert=True)

# Обновляем successful_payment для обработки deposit
@bot.message_handler(content_types=['successful_payment'])
def successful_payment(message):
    clear_pending_step(message.chat.id)  # Очищаем pending
    payload = message.successful_payment.invoice_payload
    cursor.execute("SELECT id, user_id, amount FROM payments WHERE payload = ? AND sub_type = 'deposit' AND status = 'pending'", (payload,))
    row = cursor.fetchone()
    if row:
        payment_id, user_id, amount = row
        if user_id == message.from_user.id:
            cursor.execute("UPDATE payments SET status = 'paid', transaction_id = ? WHERE id = ?", (message.successful_payment.telegram_payment_charge_id, payment_id))
            conn.commit()
            deposit_amount = amount / 2  # 2 stars = 1$
            user = get_user(user_id)
            update_user(user_id, card_balance=user['card_balance'] + deposit_amount)
            cursor.execute("INSERT INTO deposit_history (user_id, amount, created_at, request_id) VALUES (?, ?, ?, ?)",
                           (user_id, deposit_amount, datetime.now(tz), payment_id))
            conn.commit()
            bot.send_message(message.chat.id, f"Счет пополнен на {deposit_amount}$!")
            # Возвращаем в карту
            display_card(message.chat.id, message.message_id)

# ... (остальной код без изменений)

@bot.callback_query_handler(func=lambda call: call.data == "transfer_money")
def transfer_money(call):
    clear_pending_step(call.message.chat.id)  # Очищаем pending перед новым input
    caption = "Введите юзернейм сумма"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="card_settings"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    register_next_step(call.message.chat.id, process_transfer_money, call.message.message_id)

def process_transfer_money(message, message_id):
    text = message.text.split()
    if len(text) != 2 or not text[1].replace('.', '', 1).isdigit():
        bot.send_message(message.chat.id, "Неверный формат")
        card_settings(_SimpleNS(message=message, from_user=message.from_user, data="card_settings"))
        return
    to_username = text[0].lstrip('@')
    amount = float(text[1])
    from_user = get_user(message.chat.id)
    if amount > from_user['card_balance'] or amount <= 0:
        bot.send_message(message.chat.id, "Недостаточно средств или неверная сумма")
        card_settings(_SimpleNS(message=message, from_user=message.from_user, data="card_settings"))
        return
    cursor.execute("SELECT id FROM users WHERE username = ?", (to_username,))
    row = cursor.fetchone()
    if not row:
        bot.send_message(message.chat.id, "Пользователь не найден")
        card_settings(_SimpleNS(message=message, from_user=message.from_user, data="card_settings"))
        return
    to_user_id = row[0]
    if to_user_id == from_user['id']:
        bot.send_message(message.chat.id, "Нельзя переводить деньги самому себе")
        card_settings(_SimpleNS(message=message, from_user=message.from_user, data="card_settings"))
        return
    to_user = get_user(to_user_id)
    if to_user['card_status'] != 'active':
        bot.send_message(message.chat.id, "Получатель не имеет активной карты")
        card_settings(_SimpleNS(message=message, from_user=message.from_user, data="card_settings"))
        return
    caption = f"Юзернейм: {to_username}\nСумма: {amount}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Перевести ✅", callback_data=f"confirm_transfer_{to_user_id}_{amount}"))
    markup.add(types.InlineKeyboardButton("Отмена ❌", callback_data="card_settings"))
    bot.edit_message_caption(caption, message.chat.id, message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_transfer_"))
def confirm_transfer(call):
    parts = call.data.split("_")
    to_user_id = int(parts[2])
    amount = float(parts[3])
    from_user_id = call.from_user.id
    from_user = get_user(from_user_id)
    if to_user_id == from_user_id:
        bot.answer_callback_query(call.id, "Нельзя переводить деньги самому себе", show_alert=True)
        return
    if amount > from_user['card_balance']:
        bot.answer_callback_query(call.id, "Недостаточно средств", show_alert=True)
        return
    to_user = get_user(to_user_id)
    update_user(from_user_id, card_balance=from_user['card_balance'] - amount)
    update_user(to_user_id, card_balance=to_user['card_balance'] + amount)
    cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (from_user_id, -amount, datetime.now(tz), 'transfer_out'))
    cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (to_user_id, amount, datetime.now(tz), 'transfer_in'))
    cursor.execute("INSERT INTO transfers (from_user_id, to_user_id, amount, timestamp) VALUES (?, ?, ?, ?)", (from_user_id, to_user_id, amount, datetime.now(tz)))
    conn.commit()
    # Notify receiver
    notify_caption = f"Зачисление денежных средств\nЮзернейм: {from_user['username']}\nСумма: {amount}\nДата: {datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')}"
    bot.send_message(to_user_id, notify_caption)
    bot.answer_callback_query(call.id, "Перевод выполнен")
    # Return to card display without check photo
    display_card(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "card_history_user")
def card_history_user(call):
    user_id = call.from_user.id
    cursor.execute("SELECT amount, timestamp, type, id FROM card_history WHERE user_id = ? ORDER BY timestamp DESC", (user_id,))
    rows = cursor.fetchall()
    if not rows:
        caption = "Нет истории"
    else:
        caption = "История операций:"
    markup = types.InlineKeyboardMarkup()
    for row in rows:
        if row[2] in ['deposit', 'transfer_in']:
            sign = '+'
        else:
            sign = '-'
        dt = row[1] if not isinstance(row[1], str) else datetime.fromisoformat(row[1])
        if row[2] == 'transfer_in':
            cursor.execute("SELECT from_user_id FROM transfers WHERE to_user_id=? AND amount=? AND timestamp=?", (user_id, row[0], row[1]))
            tr = cursor.fetchone()
            other = get_user(tr[0])['username'] if tr else ''
            text = f"{sign}{abs(row[0])} {dt.strftime('%Y-%m-%d %H:%M')} от {other}"
        elif row[2] == 'transfer_out':
            cursor.execute("SELECT to_user_id FROM transfers WHERE from_user_id=? AND amount=? AND timestamp=?", (user_id, -row[0], row[1]))
            tr = cursor.fetchone()
            other = get_user(tr[0])['username'] if tr else ''
            text = f"{sign}{abs(row[0])} {dt.strftime('%Y-%m-%d %H:%M')} кому {other}"
        else:
            text = f"{sign}{abs(row[0])} {dt.strftime('%Y-%m-%d %H:%M')} {row[2]}"
        markup.add(types.InlineKeyboardButton(text, callback_data=f"dummy_history_{row[3]}"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="card_settings"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("dummy_history_"))
def dummy_history(call):
    bot.answer_callback_query(call.id, "Информация об операции", show_alert=False)

@bot.callback_query_handler(func=lambda call: call.data == "activate_card")
def activate_card(call):
    user = get_user(call.message.chat.id)
    if user['card_status'] != 'inactive':
        bot.answer_callback_query(call.id, "Карта не готова к активации", show_alert=True)
        return
    clear_pending_step(call.message.chat.id)  # Очищаем pending перед новым input
    caption = "Придумайте пароль из 4 цифр"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="card"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, reply_markup=markup)
    register_next_step(call.message.chat.id, set_card_password, call.message.message_id)

def set_card_password(message, message_id):
    password = message.text
    if not password.isdigit() or len(password) != 4:
        bot.send_message(message.chat.id, "Неверный формат")
        show_card(_SimpleNS(message=message, from_user=message.from_user, data="card"))
        return
    user_id = message.chat.id
    card_num = generate_card_number()
    cvv = generate_cvv()
    api_token = generate_api_token(user_id)
    update_user(user_id, card_number=card_num, cvv=cvv, card_status='active', card_password=password, card_activation_date=datetime.now(tz), api_token=api_token)
    display_card(user_id, message_id)

@bot.callback_query_handler(func=lambda call: call.data == "block_card")
def block_card(call):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Подтвердить ✅", callback_data="confirm_block_card"))
    markup.add(types.InlineKeyboardButton("Отмена ❌", callback_data="card_settings"))
    bot.edit_message_caption("Подтвердите блокировку карты", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "confirm_block_card")
def confirm_block_card(call):
    user_id = call.from_user.id
    user = get_user(user_id)
    balance = user['card_balance']
    if balance > 0:
        cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (user_id, -balance, datetime.now(tz), 'withdraw'))
        conn.commit()
    update_user(user_id, card_status='blocked', block_reason='user', card_balance=0, card_activation_date=datetime.now(tz))
    bot.edit_message_caption("Карта заблокирована, баланс списан", call.message.chat.id, call.message.message_id)
    show_card(call)

@bot.callback_query_handler(func=lambda call: call.data == "api_card")
def api_card(call):
    user = get_user(call.from_user.id)
    api_token = user.get('api_token')
    caption = f"🔑 Токен вашего аккаунта в Lixcuk_robot:\n<code>{api_token}</code>\n<blockquote>⚠️ Этот токен может использоваться для управления вашей карты в Lixcuk_robot. Храните его в надежном месте.</blockquote>"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Сбросить API", callback_data="reset_api"))
    markup.add(types.InlineKeyboardButton("Назад 🔙", callback_data="card_settings"))
    bot.edit_message_caption(caption, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "reset_api")
def reset_api(call):
    user_id = call.from_user.id
    new_token = generate_api_token(user_id)
    update_user(user_id, api_token=new_token)
    api_card(call)




@bot.message_handler(content_types=['text'])
def handle_pending(message):
    chat_id = message.chat.id
    if chat_id in pending_steps:
        handler, args = pending_steps.pop(chat_id)
        handler(message, *args)
        return
    # Если нет pending, отправляем текст помощи
    help_text = "Вы можете управлять мной, отправляя следующие команды:\n\n 🔃 /start-перезапуск бота\n 🗣️ /hold- смотреть холды\n 🗑️/del- удалить номер\n 🔄/menu- обноваить меню"
    bot.send_message(chat_id, help_text)

@bot.message_handler(commands=['hold'])
def hold(message):
    clear_pending_step(message.chat.id)
    successful = get_successful(message.chat.id)
    text = "\n".join(f"{item['phone_number']} ({item['type']}) холд: {item['hold_time']}" for item in successful if item['hold_time'])
    bot.send_message(message.chat.id, text or f"Нет холдов >= {MIN_HOLD_MINUTES} мин")

@bot.message_handler(commands=['del'])
def del_number(message):
    clear_pending_step(message.chat.id)
    phone = message.text.split()[1] if len(message.text.split()) > 1 else None
    if not phone:
        bot.send_message(message.chat.id, "Формат /del номер")
        return
    cursor.execute("DELETE FROM queue WHERE phone_number = ? AND user_id = ?", (phone, message.chat.id))
    conn.commit()
    bot.send_message(message.chat.id, "Номер удален" if cursor.rowcount > 0 else "Номер не найден")
    log_action(message.chat.id, f"Удалил номер {phone}")

@bot.message_handler(commands=['menu'])
def menu(message):
    clear_pending_step(message.chat.id)
    show_main_menu(message.chat.id)

@bot.inline_handler(func=lambda query: True)
def inline_query(query):
    if not query.query.replace('.', '', 1).isdigit():
        return
    amount = float(query.query)
    user_id = query.from_user.id
    user = get_user(user_id)
    if not user or user['card_status'] != 'active' or amount < 1 or amount > user['card_balance']:
        results = [types.InlineQueryResultArticle(id=str(uuid.uuid4()), title="❌ Недостаточно средств или карта не активна", input_message_content=types.InputTextMessageContent("❌ Ошибка создания чека."))]
        bot.answer_inline_query(query.id, results)
        return
    unique_code = str(uuid.uuid4())
    cursor.execute("INSERT INTO checks (creator_id, amount, unique_code) VALUES (?, ?, ?)", (user_id, amount, unique_code))
    conn.commit()
    check_id = cursor.lastrowid
    update_user(user_id, card_balance=user['card_balance'] - amount)
    cursor.execute("INSERT INTO card_history (user_id, amount, timestamp, type) VALUES (?, ?, ?, ?)", (user_id, -amount, datetime.now(tz), 'check_create_inline'))
    conn.commit()
    link = f"https://t.me/{bot.get_me().username}?start=check_{unique_code}"
    caption = f"🦋 Чек на {amount} USDT 🪙"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Получить ✅", url=link))
    results = [types.InlineQueryResultArticle(id=str(uuid.uuid4()), title=f"Чек на {amount} USDT", input_message_content=types.InputTextMessageContent(caption), reply_markup=markup)]
    bot.answer_inline_query(query.id, results)

bot.infinity_polling()