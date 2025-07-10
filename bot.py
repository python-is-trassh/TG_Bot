import os
import logging
import requests
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
import asyncpg
from dotenv import load_dotenv
from datetime import datetime

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация
API_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(','))) if os.getenv('ADMIN_IDS') else []
DATABASE_URL = os.getenv('DATABASE_URL')
BITCOIN_WALLET = os.getenv('BITCOIN_WALLET')

# Инициализация бота
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Состояния
class AdminStates(StatesGroup):
    waiting_category_name = State()
    waiting_product_name = State()
    waiting_product_description = State()
    waiting_product_price = State()
    waiting_product_content = State()
    waiting_product_locations = State()
    waiting_about_text = State()

class UserStates(StatesGroup):
    waiting_payment = State()

# Подключение к БД
async def create_db_connection():
    try:
        return await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        logger.error(f"Ошибка подключения к БД: {e}")
        return None

# Инициализация БД
async def init_db():
    conn = await create_db_connection()
    if not conn:
        return False

    try:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS categories (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL UNIQUE,
                is_active BOOLEAN DEFAULT TRUE
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
                name VARCHAR(100) NOT NULL,
                description TEXT,
                price DECIMAL(10, 2) NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                is_active BOOLEAN DEFAULT TRUE,
                UNIQUE(category_id, name)
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS locations (
                id SERIAL PRIMARY KEY,
                product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
                name VARCHAR(100) NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0,
                UNIQUE(product_id, name)
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                product_id INTEGER REFERENCES products(id),
                location_id INTEGER REFERENCES locations(id),
                user_id BIGINT NOT NULL,
                bitcoin_address VARCHAR(100) NOT NULL,
                amount DECIMAL(10, 8) NOT NULL,
                content TEXT NOT NULL,
                is_paid BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS shop_info (
                id INTEGER PRIMARY KEY DEFAULT 1,
                about_text TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # Добавляем текст "о магазине" по умолчанию
        await conn.execute('''
            INSERT INTO shop_info (about_text) 
            VALUES ('Добро пожаловать в наш магазин!')
            ON CONFLICT (id) DO NOTHING
        ''')

        logger.info("База данных инициализирована")
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        return False
    finally:
        await conn.close()

# Проверка Bitcoin платежа
async def check_bitcoin_payment(address, amount):
    try:
        url = f"https://blockchain.info/rawaddr/{address}"
        response = requests.get(url)
        data = response.json()
        
        total_received = data['total_received'] / 100000000  # Конвертация из сатоши
        
        # Проверяем последние транзакции (максимум за последние 2 часа)
        for tx in data['txs']:
            tx_time = datetime.fromtimestamp(tx['time'])
            if (datetime.now() - tx_time).total_seconds() > 7200:
                continue
                
            for output in tx['out']:
                if output['addr'] == address and output['value'] / 100000000 >= amount:
                    return True
        return False
    except Exception as e:
        logger.error(f"Ошибка проверки Bitcoin платежа: {e}")
        return False

# ========== КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ==========
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("🛍️ Каталог", "ℹ️ О магазине")
    
    await message.answer(
        "👋 Добро пожаловать в наш магазин!\n"
        "Выберите действие:",
        reply_markup=keyboard
    )

@dp.message_handler(text="ℹ️ О магазине")
async def show_about(message: types.Message):
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        return
    
    try:
        about_text = await conn.fetchval("SELECT about_text FROM shop_info WHERE id = 1")
        await message.answer(about_text)
    except Exception as e:
        logger.error(f"Ошибка получения информации о магазине: {e}")
        await message.answer("Информация о магазине временно недоступна")
    finally:
        await conn.close()

@dp.message_handler(text="🛍️ Каталог")
async def show_categories(message: types.Message):
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        return
    
    try:
        categories = await conn.fetch(
            "SELECT id, name FROM categories WHERE is_active = TRUE ORDER BY name"
        )
        
        if not categories:
            await message.answer("Категории товаров пока отсутствуют")
            return
        
        keyboard = types.InlineKeyboardMarkup()
        for category in categories:
            keyboard.add(types.InlineKeyboardButton(
                category['name'],
                callback_data=f"category_{category['id']}"
            ))
        
        await message.answer(
            "📂 Выберите категорию:",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка загрузки категорий: {e}")
        await message.answer("Ошибка загрузки категорий")
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('category_'))
async def show_category_products(callback_query: types.CallbackQuery):
    category_id = int(callback_query.data.split('_')[1])
    conn = await create_db_connection()
    if not conn:
        await callback_query.message.answer("Ошибка подключения к БД")
        return
    
    try:
        category_name = await conn.fetchval(
            "SELECT name FROM categories WHERE id = $1",
            category_id
        )
        
        products = await conn.fetch(
            "SELECT id, name, price FROM products WHERE category_id = $1 AND is_active = TRUE ORDER BY name",
            category_id
        )
        
        if not products:
            await callback_query.message.answer("В этой категории пока нет товаров")
            return
        
        keyboard = types.InlineKeyboardMarkup()
        for product in products:
            keyboard.add(types.InlineKeyboardButton(
                f"{product['name']} - {product['price']} BTC",
                callback_data=f"product_{product['id']}"
            ))
        
        await callback_query.message.edit_text(
            f"📦 Категория: {category_name}\n\n"
            "Выберите товар:",
            reply_markup=keyboard
        )
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Ошибка загрузки товаров: {e}")
        await callback_query.message.answer("Ошибка загрузки товаров")
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('product_'))
async def show_product_details(callback_query: types.CallbackQuery):
    product_id = int(callback_query.data.split('_')[1])
    conn = await create_db_connection()
    if not conn:
        await callback_query.message.answer("Ошибка подключения к БД")
        return
    
    try:
        product = await conn.fetchrow(
            "SELECT p.id, p.name, p.description, p.price, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "WHERE p.id = $1",
            product_id
        )
        
        locations = await conn.fetch(
            "SELECT id, name, quantity FROM locations "
            "WHERE product_id = $1 AND quantity > 0",
            product_id
        )
        
        if not locations:
            await callback_query.answer("Нет доступных локаций для этого товара")
            return
        
        text = (
            f"📦 <b>{product['name']}</b>\n"
            f"📂 Категория: {product['category_name']}\n"
            f"💰 Цена: <b>{product['price']} BTC</b>\n\n"
            f"📝 Описание:\n{product['description']}\n\n"
            "📍 Выберите локацию:"
        )
        
        keyboard = types.InlineKeyboardMarkup()
        for loc in locations:
            keyboard.add(types.InlineKeyboardButton(
                f"{loc['name']} (доступно: {loc['quantity']})",
                callback_data=f"location_{loc['id']}"
            ))
        
        await callback_query.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Ошибка загрузки товара: {e}")
        await callback_query.message.answer("Ошибка загрузки товара")
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('location_'))
async def process_location_selection(callback_query: types.CallbackQuery):
    location_id = int(callback_query.data.split('_')[1])
    conn = await create_db_connection()
    if not conn:
        await callback_query.message.answer("Ошибка подключения к БД")
        return
    
    try:
        location = await conn.fetchrow(
            "SELECT l.id, l.name, l.quantity, p.id as product_id, p.name as product_name, p.price "
            "FROM locations l JOIN products p ON l.product_id = p.id "
            "WHERE l.id = $1",
            location_id
        )
        
        if not location or location['quantity'] <= 0:
            await callback_query.answer("Эта локация больше недоступна")
            return
        
        # Генерируем уникальный Bitcoin адрес для платежа
        payment_address = BITCOIN_WALLET
        
        await callback_query.message.edit_text(
            f"💳 Оформление заказа:\n\n"
            f"📦 Товар: <b>{location['product_name']}</b>\n"
            f"📍 Локация: <b>{location['name']}</b>\n"
            f"💰 Сумма к оплате: <b>{location['price']} BTC</b>\n\n"
            f"Отправьте указанную сумму на Bitcoin адрес:\n"
            f"<code>{payment_address}</code>\n\n"
            "После оплаты нажмите кнопку ниже для проверки платежа.",
            parse_mode="HTML"
        )
        
        # Сохраняем информацию о заказе
        await UserStates.waiting_payment.set()
        state = dp.current_state(user=callback_query.from_user.id, chat=callback_query.message.chat.id)
        await state.update_data(
            product_id=location['product_id'],
            location_id=location_id,
            payment_address=payment_address,
            amount=location['price']
        )
        
    except Exception as e:
        logger.error(f"Ошибка оформления заказа: {e}")
        await callback_query.message.answer("Ошибка оформления заказа")
    finally:
        await conn.close()

@dp.message_handler(state=UserStates.waiting_payment)
async def check_payment(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        await state.finish()
        return
    
    try:
        # Проверяем платеж
        is_paid = await check_bitcoin_payment(data['payment_address'], data['amount'])
        
        if not is_paid:
            await message.answer("❌ Платеж не обнаружен. Пожалуйста, попробуйте позже или свяжитесь с поддержкой.")
            return
        
        # Получаем контент товара
        product = await conn.fetchrow(
            "SELECT p.content, l.name as location_name "
            "FROM products p JOIN locations l ON p.id = l.product_id "
            "WHERE l.id = $1",
            data['location_id']
        )
        
        if not product:
            await message.answer("❌ Ошибка: товар не найден")
            await state.finish()
            return
        
        # Помечаем локацию как проданную (уменьшаем количество)
        await conn.execute(
            "UPDATE locations SET quantity = quantity - 1 WHERE id = $1",
            data['location_id']
        )
        
        # Сохраняем заказ
        await conn.execute(
            "INSERT INTO orders (product_id, location_id, user_id, bitcoin_address, amount, content, is_paid) "
            "VALUES ($1, $2, $3, $4, $5, $6, TRUE)",
            data['product_id'],
            data['location_id'],
            message.from_user.id,
            data['payment_address'],
            data['amount'],
            product['content']
        )
        
        # Отправляем контент пользователю
        await message.answer(
            "✅ Платеж подтвержден! Ваш товар:\n\n"
            f"{product['content']}\n\n"
            "Спасибо за покупку!",
            parse_mode="HTML"
        )
        
        # Уведомляем администраторов
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🛒 Новый заказ!\n"
                    f"👤 Пользователь: @{message.from_user.username or message.from_user.id}\n"
                    f"💰 Сумма: {data['amount']} BTC\n"
                    f"📍 Локация: {product['location_name']}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления админа: {e}")
        
    except Exception as e:
        logger.error(f"Ошибка обработки платежа: {e}")
        await message.answer("❌ Произошла ошибка при обработке платежа")
    finally:
        await conn.close()
        await state.finish()

# ========== АДМИН ПАНЕЛЬ ==========
@dp.message_handler(commands=['admin'])
async def admin_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещен")
        return
    
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("➕ Добавить категорию", "➖ Удалить категорию")
    keyboard.row("📦 Добавить товар", "🗑 Удалить товар")
    keyboard.row("📍 Управление локациями", "ℹ️ Редактировать 'О магазине'")
    keyboard.row("🔙 В меню")
    
    await message.answer(
        "⚙️ <b>Админ панель</b>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@dp.message_handler(text="🔙 В меню")
async def back_to_menu(message: types.Message):
    await cmd_start(message)

# Добавление категории
@dp.message_handler(text="➕ Добавить категорию")
async def add_category_start(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    await AdminStates.waiting_category_name.set()
    await message.answer(
        "Введите название новой категории:",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message_handler(state=AdminStates.waiting_category_name)
async def add_category_finish(message: types.Message, state: FSMContext):
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        await state.finish()
        return
    
    try:
        await conn.execute(
            "INSERT INTO categories (name) VALUES ($1)",
            message.text
        )
        await message.answer(f"✅ Категория '{message.text}' добавлена")
    except asyncpg.UniqueViolationError:
        await message.answer("❌ Категория с таким названием уже существует")
    except Exception as e:
        logger.error(f"Ошибка добавления категории: {e}")
        await message.answer("❌ Ошибка добавления категории")
    finally:
        await conn.close()
        await state.finish()
        await admin_panel(message)

# Удаление категории
@dp.message_handler(text="➖ Удалить категорию")
async def delete_category_start(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        return
    
    try:
        categories = await conn.fetch(
            "SELECT id, name FROM categories ORDER BY name"
        )
        
        if not categories:
            await message.answer("Нет категорий для удаления")
            return
        
        keyboard = types.InlineKeyboardMarkup()
        for category in categories:
            keyboard.add(types.InlineKeyboardButton(
                category['name'],
                callback_data=f"deletecat_{category['id']}"
            ))
        
        await message.answer(
            "Выберите категорию для удаления:",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка загрузки категорий: {e}")
        await message.answer("Ошибка загрузки категорий")
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('deletecat_'))
async def delete_category_finish(callback_query: types.CallbackQuery):
    category_id = int(callback_query.data.split('_')[1])
    conn = await create_db_connection()
    if not conn:
        await callback_query.message.answer("Ошибка подключения к БД")
        return
    
    try:
        category_name = await conn.fetchval(
            "SELECT name FROM categories WHERE id = $1",
            category_id
        )
        
        await conn.execute(
            "DELETE FROM categories WHERE id = $1",
            category_id
        )
        
        await callback_query.message.edit_text(
            f"✅ Категория '{category_name}' удалена"
        )
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Ошибка удаления категории: {e}")
        await callback_query.message.answer("❌ Ошибка удаления категории")
    finally:
        await conn.close()

# Добавление товара
@dp.message_handler(text="📦 Добавить товар")
async def add_product_start(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        return
    
    try:
        categories = await conn.fetch(
            "SELECT id, name FROM categories ORDER BY name"
        )
        
        if not categories:
            await message.answer("Сначала создайте категорию")
            return
        
        keyboard = types.InlineKeyboardMarkup()
        for category in categories:
            keyboard.add(types.InlineKeyboardButton(
                category['name'],
                callback_data=f"addprod_{category['id']}"
            ))
        
        await message.answer(
            "Выберите категорию для товара:",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка загрузки категорий: {e}")
        await message.answer("Ошибка загрузки категорий")
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('addprod_'))
async def add_product_category(callback_query: types.CallbackQuery):
    category_id = int(callback_query.data.split('_')[1])
    
    await AdminStates.waiting_product_name.set()
    state = dp.current_state(user=callback_query.from_user.id, chat=callback_query.message.chat.id)
    await state.update_data(category_id=category_id)
    
    await callback_query.message.edit_text(
        "Введите название товара:"
    )
    await callback_query.answer()

@dp.message_handler(state=AdminStates.waiting_product_name)
async def add_product_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await AdminStates.waiting_product_description.set()
    await message.answer("Введите описание товара:")

@dp.message_handler(state=AdminStates.waiting_product_description)
async def add_product_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await AdminStates.waiting_product_price.set()
    await message.answer("Введите цену товара (в BTC):")

@dp.message_handler(state=AdminStates.waiting_product_price)
async def add_product_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text)
        if price <= 0:
            raise ValueError
        
        await state.update_data(price=price)
        await AdminStates.waiting_product_content.set()
        await message.answer("Введите контент товара (текст/ссылка, который получит пользователь после оплаты):")
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректную цену (число больше 0)")

@dp.message_handler(state=AdminStates.waiting_product_content)
async def add_product_content(message: types.Message, state: FSMContext):
    await state.update_data(content=message.text)
    await AdminStates.waiting_product_locations.set()
    await message.answer("Введите локации для товара (каждая локация с новой строки в формате: 'Название=Количество'):\n\nПример:\nМосква=5\nСанкт-Петербург=3")

@dp.message_handler(state=AdminStates.waiting_product_locations)
async def add_product_locations(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        await state.finish()
        return
    
    try:
        # Добавляем товар
        product = await conn.fetchrow(
            "INSERT INTO products (category_id, name, description, price, content) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            data['category_id'],
            data['name'],
            data['description'],
            data['price'],
            data['content']
        )
        
        # Добавляем локации
        locations = message.text.split('\n')
        for loc in locations:
            if '=' in loc:
                name, quantity = loc.split('=', 1)
                name = name.strip()
                try:
                    quantity = int(quantity.strip())
                    if quantity > 0:
                        await conn.execute(
                            "INSERT INTO locations (product_id, name, quantity) "
                            "VALUES ($1, $2, $3)",
                            product['id'],
                            name,
                            quantity
                        )
                except ValueError:
                    pass
        
        await message.answer(f"✅ Товар '{data['name']}' успешно добавлен!")
    except asyncpg.UniqueViolationError:
        await message.answer("❌ Товар с таким названием уже существует в этой категории")
    except Exception as e:
        logger.error(f"Ошибка добавления товара: {e}")
        await message.answer("❌ Ошибка добавления товара")
    finally:
        await conn.close()
        await state.finish()
        await admin_panel(message)

# Удаление товара
@dp.message_handler(text="🗑 Удалить товар")
async def delete_product_start(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        return
    
    try:
        categories = await conn.fetch(
            "SELECT id, name FROM categories ORDER BY name"
        )
        
        if not categories:
            await message.answer("Нет категорий с товарами")
            return
        
        keyboard = types.InlineKeyboardMarkup()
        for category in categories:
            keyboard.add(types.InlineKeyboardButton(
                category['name'],
                callback_data=f"delprodcat_{category['id']}"
            ))
        
        await message.answer(
            "Выберите категорию для удаления товара:",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка загрузки категорий: {e}")
        await message.answer("Ошибка загрузки категорий")
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('delprodcat_'))
async def delete_product_category(callback_query: types.CallbackQuery):
    category_id = int(callback_query.data.split('_')[1])
    conn = await create_db_connection()
    if not conn:
        await callback_query.message.answer("Ошибка подключения к БД")
        return
    
    try:
        products = await conn.fetch(
            "SELECT id, name FROM products WHERE category_id = $1 ORDER BY name",
            category_id
        )
        
        if not products:
            await callback_query.message.edit_text("В этой категории нет товаров")
            return
        
        keyboard = types.InlineKeyboardMarkup()
        for product in products:
            keyboard.add(types.InlineKeyboardButton(
                product['name'],
                callback_data=f"deleteprod_{product['id']}"
            ))
        
        await callback_query.message.edit_text(
            "Выберите товар для удаления:",
            reply_markup=keyboard
        )
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Ошибка загрузки товаров: {e}")
        await callback_query.message.answer("Ошибка загрузки товаров")
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('deleteprod_'))
async def delete_product_finish(callback_query: types.CallbackQuery):
    product_id = int(callback_query.data.split('_')[1])
    conn = await create_db_connection()
    if not conn:
        await callback_query.message.answer("Ошибка подключения к БД")
        return
    
    try:
        product_name = await conn.fetchval(
            "SELECT name FROM products WHERE id = $1",
            product_id
        )
        
        await conn.execute(
            "DELETE FROM products WHERE id = $1",
            product_id
        )
        
        await callback_query.message.edit_text(
            f"✅ Товар '{product_name}' удален"
        )
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Ошибка удаления товара: {e}")
        await callback_query.message.answer("❌ Ошибка удаления товара")
    finally:
        await conn.close()

# Управление локациями
@dp.message_handler(text="📍 Управление локациями")
async def manage_locations_start(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        return
    
    try:
        products = await conn.fetch(
            "SELECT p.id, p.name, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "ORDER BY c.name, p.name"
        )
        
        if not products:
            await message.answer("Нет товаров для управления локациями")
            return
        
        keyboard = types.InlineKeyboardMarkup()
        for product in products:
            keyboard.add(types.InlineKeyboardButton(
                f"{product['category_name']} - {product['name']}",
                callback_data=f"manageloc_{product['id']}"
            ))
        
        await message.answer(
            "Выберите товар для управления локациями:",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка загрузки товаров: {e}")
        await message.answer("Ошибка загрузки товаров")
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('manageloc_'))
async def manage_locations_product(callback_query: types.CallbackQuery):
    product_id = int(callback_query.data.split('_')[1])
    conn = await create_db_connection()
    if not conn:
        await callback_query.message.answer("Ошибка подключения к БД")
        return
    
    try:
        product = await conn.fetchrow(
            "SELECT p.name, c.name as category_name "
            "FROM products p JOIN categories c ON p.category_id = c.id "
            "WHERE p.id = $1",
            product_id
        )
        
        locations = await conn.fetch(
            "SELECT id, name, quantity FROM locations "
            "WHERE product_id = $1 ORDER BY name",
            product_id
        )
        
        text = (
            f"📍 Управление локациями для товара:\n"
            f"📂 Категория: {product['category_name']}\n"
            f"📦 Товар: {product['name']}\n\n"
            "Текущие локации:\n"
        )
        
        if locations:
            for loc in locations:
                text += f"- {loc['name']}: {loc['quantity']} шт.\n"
        else:
            text += "Нет локаций\n"
        
        keyboard = types.InlineKeyboardMarkup()
        keyboard.row(
            types.InlineKeyboardButton("➕ Добавить локацию", callback_data=f"addloc_{product_id}"),
            types.InlineKeyboardButton("➖ Удалить локацию", callback_data=f"removeloc_{product_id}")
        )
        keyboard.row(
            types.InlineKeyboardButton("✏️ Изменить количество", callback_data=f"editloc_{product_id}")
        )
        
        await callback_query.message.edit_text(
            text,
            reply_markup=keyboard
        )
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Ошибка загрузки локаций: {e}")
        await callback_query.message.answer("Ошибка загрузки локаций")
    finally:
        await conn.close()

# Редактирование информации "О магазине"
@dp.message_handler(text="ℹ️ Редактировать 'О магазине'")
async def edit_about_start(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        return
    
    try:
        about_text = await conn.fetchval(
            "SELECT about_text FROM shop_info WHERE id = 1"
        )
        
        await AdminStates.waiting_about_text.set()
        state = dp.current_state(user=message.from_user.id, chat=message.chat.id)
        await state.update_data(current_about=about_text)
        
        await message.answer(
            f"Текущий текст 'О магазине':\n\n{about_text}\n\n"
            "Введите новый текст:",
            reply_markup=types.ReplyKeyboardRemove()
        )
    except Exception as e:
        logger.error(f"Ошибка загрузки информации: {e}")
        await message.answer("Ошибка загрузки информации")
    finally:
        await conn.close()

@dp.message_handler(state=AdminStates.waiting_about_text)
async def edit_about_finish(message: types.Message, state: FSMContext):
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        await state.finish()
        return
    
    try:
        await conn.execute(
            "UPDATE shop_info SET about_text = $1, updated_at = NOW() WHERE id = 1",
            message.text
        )
        
        await message.answer("✅ Текст 'О магазине' обновлен")
    except Exception as e:
        logger.error(f"Ошибка обновления информации: {e}")
        await message.answer("❌ Ошибка обновления информации")
    finally:
        await conn.close()
        await state.finish()
        await admin_panel(message)

# ========== ЗАПУСК БОТА ==========
async def on_startup(dp):
    logger.info("Запуск бота...")
    if await init_db():
        logger.info("База данных готова")
    else:
        logger.error("Ошибка инициализации БД")
    
    # Уведомление админов
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, "✅ Бот запущен")
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")

async def on_shutdown(dp):
    logger.info("Остановка бота...")
    await dp.storage.close()
    await dp.storage.wait_closed()
    logger.info("Бот остановлен")

if __name__ == '__main__':
    executor.start_polling(
        dp,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True
                          )
