import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
import asyncpg
from dotenv import load_dotenv
from typing import List, Dict, Any, Optional

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
class Database:
    @staticmethod
    async def create_connection():
        return await asyncpg.connect(DATABASE_URL)

    @staticmethod
    async def init_db():
        conn = await Database.create_connection()
        try:
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
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS product_items (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
                    location VARCHAR(100) NOT NULL,
                    unique_code VARCHAR(50) UNIQUE NOT NULL,
                    is_sold BOOLEAN DEFAULT FALSE,
                    sold_at TIMESTAMP,
                    sold_to BIGINT
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS carts (
                    user_id BIGINT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS cart_items (
                    id SERIAL PRIMARY KEY,
                    cart_user_id BIGINT REFERENCES carts(user_id) ON DELETE CASCADE,
                    product_item_id INTEGER REFERENCES product_items(id),
                    added_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
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
            
            logger.info("Database tables created successfully")
        except Exception as e:
            logger.error(f"Error creating database tables: {e}")
            raise
        finally:
            await conn.close()

    @staticmethod
    async def execute(query: str, *args):
        conn = await Database.create_connection()
        try:
            return await conn.execute(query, *args)
        finally:
            await conn.close()

    @staticmethod
    async def fetch(query: str, *args):
        conn = await Database.create_connection()
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()

    @staticmethod
    async def fetchrow(query: str, *args):
        conn = await Database.create_connection()
        try:
            return await conn.fetchrow(query, *args)
        finally:
            await conn.close()

    @staticmethod
    async def fetchval(query: str, *args):
        conn = await Database.create_connection()
        try:
            return await conn.fetchval(query, *args)
        finally:
            await conn.close()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
async def get_or_create_cart(user_id: int) -> Dict[str, Any]:
    cart = await Database.fetchrow(
        'INSERT INTO carts (user_id) VALUES ($1) '
        'ON CONFLICT (user_id) DO UPDATE SET updated_at = NOW() '
        'RETURNING *', 
        user_id
    )
    return cart

async def add_to_cart(user_id: int, product_item_id: int) -> bool:
    item = await Database.fetchrow(
        'SELECT * FROM product_items WHERE id = $1 AND is_sold = FALSE',
        product_item_id
    )
    if not item:
        return False
    
    await Database.execute(
        'INSERT INTO cart_items (cart_user_id, product_item_id) VALUES ($1, $2)',
        user_id, product_item_id
    )
    return True

async def get_cart_contents(user_id: int) -> List[Dict[str, Any]]:
    return await Database.fetch('''
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

async def clear_cart(user_id: int):
    await Database.execute('DELETE FROM cart_items WHERE cart_user_id = $1', user_id)

async def create_order(user_id: int, cart_items: List[Dict[str, Any]]) -> Optional[int]:
    total = sum(item['price'] for item in cart_items)
    
    try:
        # Начинаем транзакцию
        async with (await Database.create_connection()).transaction():
            order = await Database.fetchrow(
                'INSERT INTO orders (user_id, total_amount) '
                'VALUES ($1, $2) RETURNING id',
                user_id, total
            )
            
            for item in cart_items:
                await Database.execute(
                    'INSERT INTO order_items '
                    '(order_id, product_item_id, product_name, location, price) '
                    'VALUES ($1, $2, $3, $4, $5)',
                    order['id'], item['product_item_id'], item['name'], 
                    item['location'], item['price']
                )
                
                await Database.execute(
                    'UPDATE product_items '
                    'SET is_sold = TRUE, sold_at = NOW(), sold_to = $1 '
                    'WHERE id = $2',
                    user_id, item['product_item_id']
                )
            
            await clear_cart(user_id)
            return order['id']
    except Exception as e:
        logger.error(f"Error creating order: {e}")
        return None

# ========== КОМАНДЫ ==========
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    try:
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
    except Exception as e:
        logger.error(f"Error in cmd_start: {e}")
        await message.answer("Произошла ошибка. Пожалуйста, попробуйте позже.")

# ========== АДМИН-ПАНЕЛЬ ==========
@dp.message_handler(text="⚙️ Админ-панель")
async def admin_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещен")
        return
    
    try:
        keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
        buttons = ["📦 Добавить товар", "📝 Список товаров", "📊 Статистика", "🔙 Назад"]
        keyboard.add(*buttons)
        
        await message.answer("Админ-панель:", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error in admin_panel: {e}")
        await message.answer("Ошибка доступа к админ-панели")

# ========== КАТАЛОГ ==========
@dp.message_handler(text="🛍️ Каталог")
async def show_catalog(message: types.Message):
    try:
        products = await Database.fetch(
            'SELECT * FROM products WHERE is_active = TRUE ORDER BY created_at DESC'
        )
        
        if not products:
            await message.answer("Каталог товаров пуст")
            return
        
        for product in products:
            available = await Database.fetchval(
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
        logger.error(f"Error showing catalog: {e}")
        await message.answer("Произошла ошибка при загрузке каталога")

# ========== КОРЗИНА ==========
@dp.message_handler(text="🛒 Корзина")
async def show_cart(message: types.Message):
    try:
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
    except Exception as e:
        logger.error(f"Error showing cart: {e}")
        await message.answer("Произошла ошибка при загрузке корзины")

# ========== ОБРАБОТЧИКИ CALLBACK ==========
@dp.callback_query_handler(lambda c: c.data == 'clear_cart')
async def clear_cart_handler(callback_query: types.CallbackQuery):
    try:
        await clear_cart(callback_query.from_user.id)
        await callback_query.message.edit_text("Корзина очищена")
    except Exception as e:
        logger.error(f"Error clearing cart: {e}")
        await callback_query.answer("Ошибка при очистке корзины", show_alert=True)

@dp.callback_query_handler(lambda c: c.data.startswith('add_to_cart:'))
async def add_to_cart_handler(callback_query: types.CallbackQuery):
    try:
        item_id = int(callback_query.data.split(':')[1])
        success = await add_to_cart(callback_query.from_user.id, item_id)
        if success:
            await callback_query.answer("Товар добавлен в корзину!")
        else:
            await callback_query.answer("Этот товар уже продан или недоступен", show_alert=True)
    except Exception as e:
        logger.error(f"Error adding to cart: {e}")
        await callback_query.answer("Ошибка при добавлении в корзину", show_alert=True)

@dp.callback_query_handler(lambda c: c.data == 'checkout')
async def checkout_handler(callback_query: types.CallbackQuery):
    try:
        user_id = callback_query.from_user.id
        cart_items = await get_cart_contents(user_id)
        
        if not cart_items:
            await callback_query.answer("Корзина пуста!", show_alert=True)
            return
        
        order_id = await create_order(user_id, cart_items)
        if order_id:
            total = sum(item['price'] for item in cart_items)
            
            await callback_query.message.edit_text(
                f"✅ Заказ #{order_id} оформлен!\n"
                f"💰 Сумма: {total} руб.\n"
                f"📦 Товаров: {len(cart_items)}\n\n"
                "Для получения товаров свяжитесь с поддержкой.",
                reply_markup=None
            )
            
            # Уведомление администраторам
            for admin_id in ADMIN_IDS:
                await bot.send_message(
                    admin_id,
                    f"🛒 Новый заказ #{order_id}\n"
                    f"👤 Пользователь: {callback_query.from_user.full_name}\n"
                    f"💳 Сумма: {total} руб.\n"
                    f"📦 Товаров: {len(cart_items)}"
                )
        else:
            await callback_query.answer(
                "Произошла ошибка при оформлении заказа",
                show_alert=True
            )
    except Exception as e:
        logger.error(f"Error during checkout: {e}")
        await callback_query.answer(
            "Произошла ошибка при оформлении заказа",
            show_alert=True
        )

# ========== АДМИН: ДОБАВЛЕНИЕ ТОВАРА ==========
@dp.message_handler(state=AddProductStates.waiting_for_name)
async def process_product_name(message: types.Message, state: FSMContext):
    try:
        if len(message.text) > 100:
            await message.answer("Название слишком длинное (макс. 100 символов)")
            return
            
        await state.update_data(name=message.text)
        await AddProductStates.waiting_for_description.set()
        await message.answer("Введите описание товара:")
    except Exception as e:
        logger.error(f"Error processing product name: {e}")
        await message.answer("Ошибка обработки названия")
        await state.finish()

@dp.message_handler(state=AddProductStates.waiting_for_description)
async def process_product_description(message: types.Message, state: FSMContext):
    try:
        await state.update_data(description=message.text)
        await AddProductStates.waiting_for_price.set()
        await message.answer("Введите цену товара (в рублях):")
    except Exception as e:
        logger.error(f"Error processing product description: {e}")
        await message.answer("Ошибка обработки описания")
        await state.finish()

@dp.message_handler(state=AddProductStates.waiting_for_price)
async def process_product_price(message: types.Message, state: FSMContext):
    try:
        price = round(float(message.text), 2)
        if price <= 0:
            await message.answer("Цена должна быть больше нуля")
            return
            
        await state.update_data(price=price)
        await AddProductStates.waiting_for_photos.set()
        await message.answer("Отправьте фотографии товара (можно несколько):")
    except ValueError:
        await message.answer("Пожалуйста, введите корректную цену (число)")
    except Exception as e:
        logger.error(f"Error processing product price: {e}")
        await message.answer("Ошибка обработки цены")
        await state.finish()

@dp.message_handler(state=AddProductStates.waiting_for_photos, content_types=types.ContentType.PHOTO)
async def process_product_photos(message: types.Message, state: FSMContext):
    try:
        if not message.photo:
            await message.answer("Пожалуйста, отправьте фотографии")
            return
            
        photo_ids = [photo.file_id for photo in message.photo]
        await state.update_data(photo_ids=photo_ids)
        
        data = await state.get_data()
        
        product = await Database.fetchrow(
            '''INSERT INTO products 
            (name, description, price, photo_ids) 
            VALUES ($1, $2, $3, $4) 
            RETURNING id, name''',
            data['name'],
            data['description'],
            data['price'],
            data['photo_ids']
        )
        
        await state.update_data(product_id=product['id'])
        await AddProductStates.waiting_for_items.set()
        
        await message.answer(
            f"✅ Товар {product['name']} создан.\n"
            "Теперь добавьте позиции. Введите локацию и уникальный код через запятую:\n"
            "Например: Москва, ABC123"
        )
    except Exception as e:
        logger.error(f"Error processing product photos: {e}")
        await message.answer("Ошибка обработки фотографий")
        await state.finish()

@dp.message_handler(state=AddProductStates.waiting_for_items)
async def process_product_items(message: types.Message, state: FSMContext):
    try:
        if message.text.lower() == '/done':
            data = await state.get_data()
            items_count = await Database.fetchval(
                'SELECT COUNT(*) FROM product_items WHERE product_id = $1',
                data['product_id']
            )
            
            await message.answer(
                f"✅ Добавление товара завершено!\n"
                f"Название: {data['name']}\n"
                f"Добавлено позиций: {items_count}"
            )
            await state.finish()
            return
            
        parts = [p.strip() for p in message.text.split(',')]
        if len(parts) < 2:
            raise ValueError("Неверный формат ввода")
            
        location = parts[0]
        unique_code = parts[1]
        
        if not location or not unique_code:
            raise ValueError("Локация и код не могут быть пустыми")
            
        data = await state.get_data()
        
        await Database.execute(
            'INSERT INTO product_items (product_id, location, unique_code) '
            'VALUES ($1, $2, $3)',
            data['product_id'], location, unique_code
        )
        
        await message.answer(
            f"✅ Позиция добавлена:\n"
            f"📍 Локация: {location}\n"
            f"🆔 Код: {unique_code}\n\n"
            f"Отправьте следующую пару или /done для завершения"
        )
    except asyncpg.UniqueViolationError:
        await message.answer("❌ Этот код уже используется. Введите другой:")
    except ValueError as e:
        await message.answer(f"❌ Ошибка: {str(e)}\nФормат: 'Локация, Код'")
    except Exception as e:
        logger.error(f"Error processing product items: {e}")
        await message.answer("❌ Критическая ошибка. Начните заново.")
        await state.finish()

# ========== ЗАПУСК ==========
async def on_startup(dp):
    try:
        logger.info("Starting bot initialization...")
        await Database.init_db()
        
        if ADMIN_IDS:
            await bot.send_message(ADMIN_IDS[0], "✅ Бот успешно запущен")
        
        logger.info("Bot started successfully")
    except Exception as e:
        logger.critical(f"Failed to start bot: {e}")
        raise

async def on_shutdown(dp):
    try:
        if ADMIN_IDS:
            await bot.send_message(ADMIN_IDS[0], "🛑 Бот остановлен")
        
        await dp.storage.close()
        await dp.storage.wait_closed()
        logger.info("Bot stopped gracefully")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")

if __name__ == '__main__':
    try:
        executor.start_polling(
            dp,
            on_startup=on_startup,
            on_shutdown=on_shutdown,
            skip_updates=True
        )
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
