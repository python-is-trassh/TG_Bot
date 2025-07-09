import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
import asyncpg
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Конфигурация
API_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(','))) if os.getenv('ADMIN_IDS') else []
DATABASE_URL = os.getenv('DATABASE_URL')

# Инициализация бота
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# ========== СОСТОЯНИЯ ==========
class AddProductStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_photos = State()
    waiting_for_items = State()

class UserStates(StatesGroup):
    waiting_for_payment = State()

# ========== БАЗА ДАННЫХ ==========
async def create_db_connection():
    return await asyncpg.connect(DATABASE_URL)

async def init_db():
    conn = await create_db_connection()
    try:
        # Таблица товаров
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                description TEXT,
                price DECIMAL(10, 2) NOT NULL,
                photo_ids TEXT[],
                created_at TIMESTAMP DEFAULT NOW(),
                is_active BOOLEAN DEFAULT TRUE
            )
        ''')
        
        # Таблица позиций
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS product_items (
                id SERIAL PRIMARY KEY,
                product_id INTEGER REFERENCES products(id),
                location VARCHAR(100) NOT NULL,
                unique_code VARCHAR(50) UNIQUE NOT NULL,
                is_sold BOOLEAN DEFAULT FALSE,
                sold_at TIMESTAMP,
                sold_to INTEGER
            )
        ''')
        
        # Таблица корзин
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS carts (
                user_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        
        # Таблица элементов корзины
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS cart_items (
                id SERIAL PRIMARY KEY,
                cart_user_id BIGINT REFERENCES carts(user_id) ON DELETE CASCADE,
                product_item_id INTEGER REFERENCES product_items(id),
                added_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        
        # Таблица заказов
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                total_amount DECIMAL(10, 2) NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                payment_details TEXT
            )
        ''')
        
        # Таблица элементов заказа
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS order_items (
                id SERIAL PRIMARY KEY,
                order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
                product_item_id INTEGER REFERENCES product_items(id),
                product_name VARCHAR(100) NOT NULL,
                location VARCHAR(100) NOT NULL,
                price DECIMAL(10, 2) NOT NULL
            )
        ''')
        
        logger.info("Таблицы базы данных успешно созданы")
    except Exception as e:
        logger.error(f"Ошибка при создании таблиц: {e}")
    finally:
        await conn.close()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
async def get_or_create_cart(user_id: int):
    conn = await create_db_connection()
    try:
        cart = await conn.fetchrow(
            'INSERT INTO carts (user_id) VALUES ($1) '
            'ON CONFLICT (user_id) DO UPDATE SET updated_at = NOW() '
            'RETURNING *', 
            user_id
        )
        return cart
    finally:
        await conn.close()

async def add_to_cart(user_id: int, product_item_id: int):
    conn = await create_db_connection()
    try:
        # Проверяем доступность товара
        item = await conn.fetchrow(
            'SELECT * FROM product_items WHERE id = $1 AND is_sold = FALSE',
            product_item_id
        )
        if not item:
            return False
        
        # Добавляем в корзину
        await conn.execute(
            'INSERT INTO cart_items (cart_user_id, product_item_id) VALUES ($1, $2)',
            user_id, product_item_id
        )
        return True
    finally:
        await conn.close()

async def get_cart_contents(user_id: int):
    conn = await create_db_connection()
    try:
        return await conn.fetch('''
            SELECT 
                ci.id as cart_item_id,
                pi.id as product_item_id,
                p.name,
                p.description,
                p.price,
                pi.location,
                pi.unique_code
            FROM cart_items ci
            JOIN product_items pi ON ci.product_item_id = pi.id
            JOIN products p ON pi.product_id = p.id
            WHERE ci.cart_user_id = $1
            ORDER BY ci.added_at DESC
        ''', user_id)
    finally:
        await conn.close()

async def clear_cart(user_id: int):
    conn = await create_db_connection()
    try:
        await conn.execute('DELETE FROM cart_items WHERE cart_user_id = $1', user_id)
    finally:
        await conn.close()

# ========== КОМАНДЫ ==========
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = ["🛍️ Каталог", "🛒 Корзина", "ℹ️ Помощь"]
    
    if message.from_user.id in ADMIN_IDS:
        buttons.append("⚙️ Админ-панель")
    
    keyboard.add(*buttons)
    
    await message.answer(
        "👋 Добро пожаловать в магазин цифровых товаров!\n"
        "Выберите действие:",
        reply_markup=keyboard
    )

@dp.message_handler(text="⚙️ Админ-панель")
async def admin_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = ["📦 Добавить товар", "📝 Список товаров", "📊 Статистика", "🔙 Назад"]
    keyboard.add(*buttons)
    
    await message.answer("Админ-панель:", reply_markup=keyboard)

# ========== КАТАЛОГ ==========
@dp.message_handler(text="🛍️ Каталог")
async def show_catalog(message: types.Message):
    conn = await create_db_connection()
    try:
        products = await conn.fetch(
            'SELECT * FROM products WHERE is_active = TRUE ORDER BY created_at DESC'
        )
        
        if not products:
            await message.answer("Каталог товаров пуст")
            return
        
        for product in products:
            available = await conn.fetchval(
                'SELECT COUNT(*) FROM product_items '
                'WHERE product_id = $1 AND is_sold = FALSE',
                product['id']
            )
            
            text = (
                f"📦 <b>{product['name']}</b>\n"
                f"💰 Цена: {product['price']} руб.\n"
                f"🛍 Доступно: {available} шт.\n"
                f"📝 {product['description']}"
            )
            
            # Отправка фото
            if product['photo_ids']:
                media = [types.InputMediaPhoto(product['photo_ids'][0], caption=text, parse_mode="HTML")]
                for photo_id in product['photo_ids'][1:]:
                    media.append(types.InputMediaPhoto(photo_id))
                
                await bot.send_media_group(message.chat.id, media)
            
            # Кнопка выбора локации
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton(
                "📍 Выбрать локацию", 
                callback_data=f"select_location:{product['id']}"
            ))
            
            await message.answer(
                "Выберите локацию для покупки:",
                reply_markup=keyboard
            )
    except Exception as e:
        logger.error(f"Ошибка при загрузке каталога: {e}")
        await message.answer("Произошла ошибка при загрузке каталога")
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('select_location:'))
async def select_location(callback_query: types.CallbackQuery):
    product_id = int(callback_query.data.split(':')[1])
    
    conn = await create_db_connection()
    try:
        locations = await conn.fetch(
            'SELECT DISTINCT location FROM product_items '
            'WHERE product_id = $1 AND is_sold = FALSE '
            'ORDER BY location',
            product_id
        )
        
        if not locations:
            await callback_query.answer("Нет доступных позиций", show_alert=True)
            return
        
        keyboard = InlineKeyboardMarkup(row_width=2)
        for loc in locations:
            keyboard.add(InlineKeyboardButton(
                loc['location'],
                callback_data=f"show_items:{product_id}:{loc['location']}"
            ))
        
        await callback_query.message.edit_text(
            "Выберите локацию:",
            reply_markup=keyboard
        )
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('show_items:'))
async def show_available_items(callback_query: types.CallbackQuery):
    _, product_id, location = callback_query.data.split(':')
    product_id = int(product_id)
    
    conn = await create_db_connection()
    try:
        # Получаем информацию о товаре
        product = await conn.fetchrow(
            'SELECT name, price FROM products WHERE id = $1',
            product_id
        )
        
        # Получаем доступные позиции
        items = await conn.fetch(
            'SELECT id, unique_code FROM product_items '
            'WHERE product_id = $1 AND location = $2 AND is_sold = FALSE '
            'ORDER BY unique_code',
            product_id, location
        )
        
        if not items:
            await callback_query.answer("Нет доступных позиций", show_alert=True)
            return
        
        text = (
            f"📦 <b>{product['name']}</b>\n"
            f"📍 Локация: {location}\n"
            f"💰 Цена: {product['price']} руб.\n"
            f"Доступные коды:"
        )
        
        keyboard = InlineKeyboardMarkup(row_width=3)
        for item in items:
            keyboard.insert(InlineKeyboardButton(
                item['unique_code'],
                callback_data=f"add_to_cart:{item['id']}"
            ))
        
        await callback_query.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    finally:
        await conn.close()

# ========== КОРЗИНА ==========
@dp.message_handler(text="🛒 Корзина")
async def show_cart(message: types.Message):
    cart_items = await get_cart_contents(message.from_user.id)
    
    if not cart_items:
        await message.answer("Ваша корзина пуста")
        return
    
    total = sum(item['price'] for item in cart_items)
    
    text = "🛒 <b>Ваша корзина</b>\n\n"
    for item in cart_items:
        text += (
            f"📦 <b>{item['name']}</b>\n"
            f"📍 Локация: {item['location']}\n"
            f"🆔 Код: {item['unique_code']}\n"
            f"💰 Цена: {item['price']} руб.\n"
            f"└── [Удалить](tg://btn/{item['cart_item_id']})\n\n"
        )
    
    text += f"💵 <b>Итого: {total} руб.</b>"
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("💳 Оформить заказ", callback_data="checkout"),
        InlineKeyboardButton("❌ Очистить корзину", callback_data="clear_cart")
    )
    
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

@dp.callback_query_handler(lambda c: c.data == 'clear_cart')
async def clear_cart_handler(callback_query: types.CallbackQuery):
    await clear_cart(callback_query.from_user.id)
    await callback_query.message.edit_text("Корзина очищена")

@dp.callback_query_handler(lambda c: c.data.startswith('add_to_cart:'))
async def add_to_cart_handler(callback_query: types.CallbackQuery):
    item_id = int(callback_query.data.split(':')[1])
    
    success = await add_to_cart(callback_query.from_user.id, item_id)
    if success:
        await callback_query.answer("Товар добавлен в корзину!")
    else:
        await callback_query.answer("Этот товар уже продан или недоступен", show_alert=True)

@dp.callback_query_handler(lambda c: c.data == 'checkout')
async def checkout(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    cart_items = await get_cart_contents(user_id)
    
    if not cart_items:
        await callback_query.answer("Корзина пуста!", show_alert=True)
        return
    
    total = sum(item['price'] for item in cart_items)
    
    # Создаем заказ
    conn = await create_db_connection()
    try:
        # Начинаем транзакцию
        async with conn.transaction():
            # Создаем заказ
            order = await conn.fetchrow(
                'INSERT INTO orders (user_id, total_amount) '
                'VALUES ($1, $2) RETURNING *',
                user_id, total
            )
            
            # Добавляем товары в заказ
            for item in cart_items:
                await conn.execute(
                    'INSERT INTO order_items '
                    '(order_id, product_item_id, product_name, location, price) '
                    'VALUES ($1, $2, $3, $4, $5)',
                    order['id'], item['product_item_id'], item['name'], 
                    item['location'], item['price']
                )
                
                # Помечаем товар как проданный
                await conn.execute(
                    'UPDATE product_items '
                    'SET is_sold = TRUE, sold_at = NOW(), sold_to = $1 '
                    'WHERE id = $2',
                    user_id, item['product_item_id']
                )
            
            # Очищаем корзину
            await conn.execute(
                'DELETE FROM cart_items WHERE cart_user_id = $1',
                user_id
            )
        
        # Уведомляем пользователя
        await callback_query.message.edit_text(
            f"✅ Заказ #{order['id']} оформлен!\n"
            f"💰 Сумма: {total} руб.\n"
            f"📦 Товаров: {len(cart_items)}\n\n"
            "Для получения товаров свяжитесь с поддержкой.",
            reply_markup=None
        )
        
        # Уведомляем администраторов
        for admin_id in ADMIN_IDS:
            await bot.send_message(
                admin_id,
                f"🛒 Новый заказ #{order['id']}\n"
                f"👤 Пользователь: {callback_query.from_user.full_name}\n"
                f"💳 Сумма: {total} руб.\n"
                f"📦 Товаров: {len(cart_items)}"
            )
    except Exception as e:
        logger.error(f"Ошибка при оформлении заказа: {e}")
        await callback_query.answer(
            "Произошла ошибка при оформлении заказа",
            show_alert=True
        )
    finally:
        await conn.close()

# ========== АДМИН: ДОБАВЛЕНИЕ ТОВАРА ==========
@dp.message_handler(text="📦 Добавить товар")
async def start_adding_product(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    await AddProductStates.waiting_for_name.set()
    await message.answer("Введите название товара:")

@dp.message_handler(state=AddProductStates.waiting_for_name)
async def process_product_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await AddProductStates.waiting_for_description.set()
    await message.answer("Введите описание товара:")

@dp.message_handler(state=AddProductStates.waiting_for_description)
async def process_product_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await AddProductStates.waiting_for_price.set()
    await message.answer("Введите цену товара (в рублях):")

@dp.message_handler(state=AddProductStates.waiting_for_price)
async def process_product_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text)
        if price <= 0:
            raise ValueError
        await state.update_data(price=price)
        await AddProductStates.waiting_for_photos.set()
        await message.answer("Отправьте фотографии товара (можно несколько):")
    except ValueError:
        await message.answer("Пожалуйста, введите корректную цену (положительное число):")

@dp.message_handler(state=AddProductStates.waiting_for_photos, content_types=types.ContentType.PHOTO)
async def process_product_photos(message: types.Message, state: FSMContext):
    photo_ids = [photo.file_id for photo in message.photo]
    await state.update_data(photo_ids=photo_ids)
    
    data = await state.get_data()
    
    conn = await create_db_connection()
    try:
        product = await conn.fetchrow(
            'INSERT INTO products (name, description, price, photo_ids) '
            'VALUES ($1, $2, $3, $4) RETURNING *',
            data['name'], data['description'], data['price'], data['photo_ids']
        )
        
        await state.update_data(product_id=product['id'])
        await AddProductStates.waiting_for_items.set()
        
        await message.answer(
            f"✅ Товар {product['name']} создан.\n"
            "Теперь добавьте позиции. Введите локацию и уникальный код через запятую:\n"
            "Например: Москва, ABC123"
        )
    except Exception as e:
        logger.error(f"Ошибка при создании товара: {e}")
        await message.answer("Произошла ошибка при создании товара")
        await state.finish()
    finally:
        await conn.close()

@dp.message_handler(state=AddProductStates.waiting_for_items)
async def process_product_items(message: types.Message, state: FSMContext):
    try:
        parts = [p.strip() for p in message.text.split(',')]
        if len(parts) < 2:
            raise ValueError
        
        location = parts[0]
        unique_code = parts[1]
        
        data = await state.get_data()
        
        conn = await create_db_connection()
        try:
            await conn.execute(
                'INSERT INTO product_items (product_id, location, unique_code) '
                'VALUES ($1, $2, $3)',
                data['product_id'], location, unique_code
            )
            
            await message.answer(
                f"✅ Позиция добавлена: {location} - {unique_code}\n"
                "Отправьте следующую пару или /done для завершения"
            )
        except asyncpg.UniqueViolationError:
            await message.answer("Этот код уже используется. Введите другой:")
        except Exception as e:
            logger.error(f"Ошибка при добавлении позиции: {e}")
            await message.answer("Произошла ошибка. Попробуйте еще раз:")
    except ValueError:
        await message.answer(
            "Неверный формат. Введите локацию и код через запятую:\n"
            "Например: Москва, ABC123"
        )

@dp.message_handler(commands=['done'], state=AddProductStates.waiting_for_items)
async def finish_adding_items(message: types.Message, state: FSMContext):
    data = await state.get_data()
    
    conn = await create_db_connection()
    try:
        items_count = await conn.fetchval(
            'SELECT COUNT(*) FROM product_items WHERE product_id = $1',
            data['product_id']
        )
        
        product = await conn.fetchrow(
            'SELECT name FROM products WHERE id = $1',
            data['product_id']
        )
        
        await message.answer(
            f"✅ Добавление товара завершено!\n"
            f"Название: {product['name']}\n"
            f"Добавлено позиций: {items_count}"
        )
    finally:
        await conn.close()
        await state.finish()

@dp.message_handler(state=AddProductStates.waiting_for_items)
async def process_product_items(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        if not all(k in data for k in ['name', 'description', 'price', 'photo_ids', 'product_id']):
            raise ValueError("Недостаточно данных для создания товара")

        # Разделяем ввод на локацию и код
        parts = message.text.split(',')
        if len(parts) < 2:
            raise ValueError("Неверный формат. Нужно: 'Локация, Код'")

        location = parts[0].strip()
        unique_code = parts[1].strip()

        if not location or not unique_code:
            raise ValueError("Локация и код не могут быть пустыми")

        conn = None
        try:
            conn = await asyncpg.connect(DATABASE_URL)
            
            # Проверяем уникальность кода
            code_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM product_items WHERE unique_code = $1)",
                unique_code
            )
            if code_exists:
                await message.answer("Этот код уже используется. Введите другой:")
                return

            # Добавляем позицию
            await conn.execute(
                """INSERT INTO product_items 
                (product_id, location, unique_code) 
                VALUES ($1, $2, $3)""",
                data['product_id'], location, unique_code
            )

            await message.answer(
                f"✅ Позиция добавлена:\n"
                f"📍 Локация: {location}\n"
                f"🆔 Код: {unique_code}\n\n"
                f"Отправьте следующую пару или /done для завершения"
            )

        except asyncpg.UniqueViolationError:
            await message.answer("❌ Этот код уже существует. Введите другой:")
        except asyncpg.PostgresError as e:
            logger.error(f"Ошибка PostgreSQL: {e}")
            await message.answer("❌ Ошибка базы данных. Попробуйте ещё раз.")
        finally:
            if conn:
                await conn.close()

    except ValueError as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}")
        await message.answer("❌ Критическая ошибка. Начните заново.")
        await state.finish()

# ========== ЗАПУСК ==========
async def on_startup(dp):
    await init_db()
    logger.info("Бот успешно запущен")

if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
