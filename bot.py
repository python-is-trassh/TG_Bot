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
class AdminStates(StatesGroup):
    waiting_product_name = State()
    waiting_product_description = State()
    waiting_product_price = State()
    waiting_product_photos = State()
    waiting_product_items = State()

# ========== КОРЗИНА ==========
class Cart:
    def __init__(self, user_id):
        self.user_id = user_id
        self.items = {}
    
    def add_item(self, product_id, quantity=1):
        if product_id in self.items:
            self.items[product_id] += quantity
        else:
            self.items[product_id] = quantity
    
    def remove_item(self, product_id, quantity=1):
        if product_id in self.items:
            if self.items[product_id] <= quantity:
                del self.items[product_id]
            else:
                self.items[product_id] -= quantity
            return True
        return False
    
    def clear(self):
        self.items = {}
    
    def get_total_items(self):
        return sum(self.items.values())
    
    async def get_cart_details(self, conn):
        if not self.items:
            return None, 0
        
        products = []
        total_price = 0
        
        for product_id, quantity in self.items.items():
            product = await conn.fetchrow(
                "SELECT id, name, price, photo_ids FROM products WHERE id = $1",
                product_id
            )
            if product:
                products.append({
                    'id': product['id'],
                    'name': product['name'],
                    'price': product['price'],
                    'quantity': quantity,
                    'photo': product['photo_ids'][0] if product['photo_ids'] else None
                })
                total_price += product['price'] * quantity
        
        return products, total_price

user_carts = {}

def get_user_cart(user_id):
    if user_id not in user_carts:
        user_carts[user_id] = Cart(user_id)
    return user_carts[user_id]

# ========== БАЗА ДАННЫХ ==========
async def create_db_connection():
    try:
        return await asyncpg.connect(DATABASE_URL)
    except Exception as e:
        logger.error(f"Ошибка подключения к БД: {e}")
        return None

async def init_db():
    conn = await create_db_connection()
    if not conn:
        return False

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
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                total_amount DECIMAL(10, 2) NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS order_items (
                id SERIAL PRIMARY KEY,
                order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
                product_id INTEGER REFERENCES products(id),
                item_id INTEGER REFERENCES product_items(id),
                price DECIMAL(10, 2) NOT NULL
            )
        ''')

        logger.info("База данных инициализирована")
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        return False
    finally:
        await conn.close()

# ========== ОСНОВНЫЕ КОМАНДЫ ==========
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = ["🛍️ Каталог", "🛒 Корзина", "ℹ️ Помощь"]
    
    if message.from_user.id in ADMIN_IDS:
        buttons.append("⚙️ Админ-панель")
    
    keyboard.add(*buttons)
    
    await message.answer(
        "👋 Добро пожаловать в магазин цифровых товаров!",
        reply_markup=keyboard
    )

@dp.message_handler(text="ℹ️ Помощь")
async def show_help(message: types.Message):
    help_text = (
        "ℹ️ <b>Справка по боту</b>\n\n"
        "🛍️ <b>Каталог</b> - просмотр товаров\n"
        "🛒 <b>Корзина</b> - ваши товары\n"
        "⚙️ <b>Админ-панель</b> - управление\n\n"
        "Для начала работы нажмите /start"
    )
    await message.answer(help_text, parse_mode="HTML")

# ========== АДМИН-ПАНЕЛЬ ==========
@dp.message_handler(text="⚙️ Админ-панель")
async def admin_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("🚫 Доступ запрещен")
        return
    
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [
        "📦 Добавить товар",
        "📝 Список товаров",
        "📊 Статистика",
        "🔙 Назад"
    ]
    keyboard.add(*buttons)
    
    await message.answer("⚙️ Админ-панель:", reply_markup=keyboard)

@dp.message_handler(text="🔙 Назад")
async def back_to_main(message: types.Message):
    await cmd_start(message)

# ========== ДОБАВЛЕНИЕ ТОВАРА ==========
@dp.message_handler(text="📦 Добавить товар")
async def start_adding_product(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    await AdminStates.waiting_product_name.set()
    await message.answer("Введите название товара:")

@dp.message_handler(state=AdminStates.waiting_product_name)
async def process_product_name(message: types.Message, state: FSMContext):
    if len(message.text) > 100:
        await message.answer("Название должно быть короче 100 символов")
        return
    
    await state.update_data(name=message.text)
    await AdminStates.waiting_product_description.set()
    await message.answer("Введите описание товара:")

@dp.message_handler(state=AdminStates.waiting_product_description)
async def process_product_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await AdminStates.waiting_product_price.set()
    await message.answer("Введите цену товара (в рублях):")

@dp.message_handler(state=AdminStates.waiting_product_price)
async def process_product_price(message: types.Message, state: FSMContext):
    try:
        price = round(float(message.text), 2)
        if price <= 0:
            await message.answer("Цена должна быть больше нуля")
            return
            
        await state.update_data(price=price)
        await AdminStates.waiting_product_photos.set()
        
        keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("✅ Завершить добавление фото")
        
        await message.answer(
            "Отправьте фотографии товара:",
            reply_markup=keyboard
        )
    except ValueError:
        await message.answer("Пожалуйста, введите корректную цену")

@dp.message_handler(content_types=types.ContentType.PHOTO, state=AdminStates.waiting_product_photos)
async def process_product_photos(message: types.Message, state: FSMContext):
    try:
        photo_id = message.photo[-1].file_id
        data = await state.get_data()
        photo_ids = data.get('photo_ids', [])
        
        if len(photo_ids) >= 10:
            await message.answer("Максимум 10 фото")
            return
            
        photo_ids.append(photo_id)
        await state.update_data(photo_ids=photo_ids)
        
        keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add("✅ Завершить добавление фото")
        keyboard.add("🖼 Просмотреть фото", "❌ Удалить последнее фото")
        
        await message.answer(
            f"Фото добавлено. Всего: {len(photo_ids)}\n"
            "Отправьте ещё фото или завершите добавление",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка обработки фото: {e}")
        await message.answer("Ошибка обработки фото")

@dp.message_handler(text="🖼 Просмотреть фото", state=AdminStates.waiting_product_photos)
async def view_added_photos(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photo_ids = data.get('photo_ids', [])
    
    if not photo_ids:
        await message.answer("Нет добавленных фото")
        return
    
    media = [types.InputMediaPhoto(photo_ids[0], caption=f"Добавленные фото (1/{len(photo_ids)})")]
    media.extend([types.InputMediaPhoto(pid) for pid in photo_ids[1:]])
    
    await bot.send_media_group(message.chat.id, media)

@dp.message_handler(text="❌ Удалить последнее фото", state=AdminStates.waiting_product_photos)
async def remove_last_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photo_ids = data.get('photo_ids', [])
    
    if not photo_ids:
        await message.answer("Нет фото для удаления")
        return
    
    photo_ids.pop()
    await state.update_data(photo_ids=photo_ids)
    await message.answer(f"Удалено. Осталось фото: {len(photo_ids)}")

@dp.message_handler(text="✅ Завершить добавление фото", state=AdminStates.waiting_product_photos)
async def finish_adding_photos(message: types.Message, state: FSMContext):
    data = await state.get_data()
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        await state.finish()
        return
    
    try:
        product = await conn.fetchrow(
            '''INSERT INTO products (name, description, price, photo_ids)
            VALUES ($1, $2, $3, $4) RETURNING id''',
            data['name'], data['description'], data['price'], data.get('photo_ids', [])
        )
        
        await state.update_data(product_id=product['id'])
        await AdminStates.waiting_product_items.set()
        
        await message.answer(
            f"✅ Товар добавлен! ID: {product['id']}\n"
            "Теперь добавьте позиции товара в формате:\n"
            "<b>Локация, УникальныйКод</b>\n"
            "Например: Москва, ABC123",
            reply_markup=types.ReplyKeyboardRemove(),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка сохранения товара: {e}")
        await message.answer("Ошибка сохранения товара")
        await state.finish()
    finally:
        await conn.close()

@dp.message_handler(state=AdminStates.waiting_product_items)
async def process_product_items(message: types.Message, state: FSMContext):
    if message.text.lower() == '/done':
        data = await state.get_data()
        conn = await create_db_connection()
        if not conn:
            await message.answer("Ошибка подключения к БД")
            await state.finish()
            return
        
        try:
            items_count = await conn.fetchval(
                "SELECT COUNT(*) FROM product_items WHERE product_id = $1",
                data.get('product_id', 0)
            )
            
            await message.answer(
                f"✅ Товар полностью добавлен!\n"
                f"Название: {data.get('name', 'N/A')}\n"
                f"Позиций: {items_count}"
            )
        finally:
            await conn.close()
            await state.finish()
        return
    
    try:
        location, code = [x.strip() for x in message.text.split(',', 1)]
        if not location or not code:
            raise ValueError
        
        data = await state.get_data()
        product_id = data.get('product_id')
        if not product_id:
            raise ValueError("Не найден ID товара")
        
        conn = await create_db_connection()
        if not conn:
            await message.answer("Ошибка подключения к БД")
            return
        
        try:
            await conn.execute(
                "INSERT INTO product_items (product_id, location, unique_code) "
                "VALUES ($1, $2, $3)",
                product_id, location, code
            )
            
            await message.answer(
                f"✅ Позиция добавлена:\n"
                f"Локация: {location}\n"
                f"Код: {code}\n\n"
                f"Отправьте следующую пару или /done для завершения"
            )
        except asyncpg.UniqueViolationError:
            await message.answer("❌ Этот код уже используется. Введите другой:")
        except Exception as e:
            logger.error(f"Ошибка добавления позиции: {e}")
            await message.answer("Ошибка при добавлении позиции")
        finally:
            await conn.close()
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Введите:\n"
            "Локация, УникальныйКод\n"
            "Пример: Москва, ABC123"
        )

# ========== КАТАЛОГ И КОРЗИНА ==========
@dp.message_handler(text="🛍️ Каталог")
async def show_catalog(message: types.Message):
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        return
    
    try:
        products = await conn.fetch(
            "SELECT * FROM products WHERE is_active = TRUE ORDER BY id DESC"
        )
        if not products:
            await message.answer("Каталог пуст")
            return
        
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        
        for product in products:
            available = await conn.fetchval(
                "SELECT COUNT(*) FROM product_items "
                "WHERE product_id = $1 AND is_sold = FALSE",
                product['id']
            )
            
            if available <= 0:
                continue
            
            btn_text = f"{product['name']} - {product['price']} руб."
            keyboard.add(types.InlineKeyboardButton(
                btn_text, callback_data=f"product_{product['id']}"
            ))
        
        await message.answer(
            "🛍️ <b>Каталог товаров</b>",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка каталога: {e}")
        await message.answer("Ошибка загрузки каталога")
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
            "SELECT * FROM products WHERE id = $1 AND is_active = TRUE",
            product_id
        )
        if not product:
            await callback_query.answer("Товар не найден")
            return
        
        available = await conn.fetchval(
            "SELECT COUNT(*) FROM product_items "
            "WHERE product_id = $1 AND is_sold = FALSE",
            product['id']
        )
        
        if available <= 0:
            await callback_query.answer("Товар закончился")
            return
        
        text = (
            f"📦 <b>{product['name']}</b>\n\n"
            f"📝 Описание:\n{product['description']}\n\n"
            f"💰 Цена: <b>{product['price']} руб.</b>\n"
            f"🛒 В наличии: <b>{available} шт.</b>"
        )
        
        keyboard = types.InlineKeyboardMarkup()
        keyboard.row(
            types.InlineKeyboardButton("➕ В корзину", callback_data=f"add_to_cart_{product['id']}"),
            types.InlineKeyboardButton("➖ Из корзины", callback_data=f"remove_from_cart_{product['id']}")
        )
        keyboard.add(types.InlineKeyboardButton("🛒 Корзина", callback_data="view_cart"))
        
        if product['photo_ids']:
            await bot.send_photo(
                callback_query.message.chat.id,
                product['photo_ids'][0],
                caption=text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                callback_query.message.chat.id,
                text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        
        await callback_query.answer()
    except Exception as e:
        logger.error(f"Ошибка товара: {e}")
        await callback_query.message.answer("Ошибка загрузки товара")
    finally:
        await conn.close()

@dp.message_handler(text="🛒 Корзина")
async def view_cart(message: types.Message):
    cart = get_user_cart(message.from_user.id)
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        return
    
    try:
        products, total_price = await cart.get_cart_details(conn)
        if not products:
            await message.answer("🛒 Ваша корзина пуста")
            return
        
        text = "🛒 <b>Ваша корзина</b>\n\n"
        for product in products:
            text += (
                f"📦 {product['name']}\n"
                f"💰 {product['price']} руб. × {product['quantity']} = "
                f"{product['price'] * product['quantity']} руб.\n\n"
            )
        
        text += f"💵 <b>Итого: {total_price} руб.</b>"
        
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("✅ Оформить заказ", callback_data="checkout"),
            types.InlineKeyboardButton("🗑️ Очистить корзину", callback_data="clear_cart")
        )
        
        if products[0]['photo']:
            await bot.send_photo(
                message.chat.id,
                products[0]['photo'],
                caption=text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        else:
            await message.answer(
                text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Ошибка корзины: {e}")
        await message.answer("Ошибка загрузки корзины")
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('add_to_cart_'))
async def add_to_cart(callback_query: types.CallbackQuery):
    product_id = int(callback_query.data.split('_')[3])
    cart = get_user_cart(callback_query.from_user.id)
    
    conn = await create_db_connection()
    if not conn:
        await callback_query.message.answer("Ошибка подключения к БД")
        return
    
    try:
        available = await conn.fetchval(
            "SELECT COUNT(*) FROM product_items "
            "WHERE product_id = $1 AND is_sold = FALSE",
            product_id
        )
        
        if available <= 0:
            await callback_query.answer("Товар закончился")
            return
        
        current_in_cart = cart.items.get(product_id, 0)
        if current_in_cart >= available:
            await callback_query.answer("Достигнуто максимальное количество")
            return
        
        cart.add_item(product_id)
        await callback_query.answer(f"Добавлено в корзину (Всего: {cart.get_total_items()})")
    except Exception as e:
        logger.error(f"Ошибка добавления в корзину: {e}")
        await callback_query.answer("Ошибка добавления")
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('remove_from_cart_'))
async def remove_from_cart(callback_query: types.CallbackQuery):
    product_id = int(callback_query.data.split('_')[3])
    cart = get_user_cart(callback_query.from_user.id)
    
    if cart.remove_item(product_id):
        await callback_query.answer(f"Удалено из корзины (Всего: {cart.get_total_items()})")
    else:
        await callback_query.answer("Этого товара нет в корзине")

@dp.callback_query_handler(lambda c: c.data == 'view_cart')
async def callback_view_cart(callback_query: types.CallbackQuery):
    await callback_query.answer()
    await view_cart(callback_query.message)

@dp.callback_query_handler(lambda c: c.data == 'clear_cart')
async def clear_cart(callback_query: types.CallbackQuery):
    cart = get_user_cart(callback_query.from_user.id)
    cart.clear()
    await callback_query.answer("Корзина очищена")
    await callback_query.message.edit_text("🛒 Ваша корзина пуста")

@dp.callback_query_handler(lambda c: c.data == 'checkout')
async def checkout(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    cart = get_user_cart(user_id)
    
    if not cart.items:
        await callback_query.answer("Корзина пуста")
        return
    
    conn = await create_db_connection()
    if not conn:
        await callback_query.message.answer("Ошибка подключения к БД")
        return
    
    try:
        products, total_price = await cart.get_cart_details(conn)
        if not products:
            await callback_query.answer("Корзина пуста")
            return
        
        # Проверка наличия
        for product in products:
            available = await conn.fetchval(
                "SELECT COUNT(*) FROM product_items "
                "WHERE product_id = $1 AND is_sold = FALSE",
                product['id']
            )
            
            if available < product['quantity']:
                await callback_query.answer(
                    f"Товара '{product['name']}' осталось только {available} шт.",
                    show_alert=True
                )
                return
        
        # Создание заказа
        order_id = await conn.fetchval(
            '''INSERT INTO orders (user_id, total_amount)
            VALUES ($1, $2) RETURNING id''',
            user_id, total_price
        )
        
        # Резервирование товаров
        for product in products:
            items = await conn.fetch(
                '''UPDATE product_items
                SET is_sold = TRUE, sold_to = $1, sold_at = NOW()
                WHERE id IN (
                    SELECT id FROM product_items
                    WHERE product_id = $2 AND is_sold = FALSE
                    LIMIT $3
                )
                RETURNING id''',
                user_id, product['id'], product['quantity']
            )
            
            for item in items:
                await conn.execute(
                    '''INSERT INTO order_items (order_id, product_id, item_id, price)
                    VALUES ($1, $2, $3, $4)''',
                    order_id, product['id'], item['id'], product['price']
                )
        
        # Формирование сообщения
        text = (
            "✅ <b>Заказ оформлен!</b>\n\n"
            f"🆔 Номер: <code>{order_id}</code>\n"
            f"💵 Сумма: <b>{total_price} руб.</b>\n\n"
            "Ваши товары:\n"
        )
        
        for product in products:
            text += f"📦 {product['name']} × {product['quantity']}\n"
        
        cart.clear()
        await callback_query.message.edit_text(text, parse_mode="HTML")
        
        # Уведомление админов
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🛒 Новый заказ #{order_id}\n"
                    f"👤 Пользователь: {callback_query.from_user.mention}\n"
                    f"💵 Сумма: {total_price} руб.",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления админа: {e}")
                
    except Exception as e:
        logger.error(f"Ошибка оформления заказа: {e}")
        await callback_query.message.answer("Ошибка оформления заказа")
    finally:
        await conn.close()

# ========== ЗАПУСК ==========
async def on_startup(dp):
    logger.info("Бот запускается...")
    if await init_db():
        logger.info("БД готова")
    else:
        logger.error("Ошибка БД")
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, "✅ Бот запущен")
        except Exception as e:
            logger.error(f"Не удалось уведомить админа: {e}")

async def on_shutdown(dp):
    logger.info("Бот останавливается...")
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
