import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
import asyncpg
from dotenv import load_dotenv

# Загрузка переменных окружения из файла .env
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),  # Логи в файл
        logging.StreamHandler()  # Логи в консоль
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация бота
API_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(','))) if os.getenv('ADMIN_IDS') else []
DATABASE_URL = os.getenv('DATABASE_URL')

# Инициализация бота и диспетчера
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# ========== СОСТОЯНИЯ (FSM) ==========
class AdminStates(StatesGroup):
    """Класс для хранения состояний админ-панели"""
    waiting_product_name = State()       # Ожидание названия товара
    waiting_product_description = State() # Ожидание описания товара
    waiting_product_price = State()      # Ожидание цены товара
    waiting_product_photos = State()     # Ожидание фотографий товара
    waiting_product_items = State()      # Ожидание добавления позиций

# ========== КОРЗИНА ==========
class Cart:
    """Класс для работы с корзиной пользователя"""
    def __init__(self, user_id):
        self.user_id = user_id
        self.items = {}  # {product_id: quantity}
    
    def add_item(self, product_id, quantity=1):
        """Добавляет товар в корзину"""
        if product_id in self.items:
            self.items[product_id] += quantity
        else:
            self.items[product_id] = quantity
    
    def remove_item(self, product_id, quantity=1):
        """Удаляет товар из корзины"""
        if product_id in self.items:
            if self.items[product_id] <= quantity:
                del self.items[product_id]
            else:
                self.items[product_id] -= quantity
            return True
        return False
    
    def clear(self):
        """Очищает корзину"""
        self.items = {}
    
    def get_total_items(self):
        """Возвращает общее количество товаров в корзине"""
        return sum(self.items.values())
    
    async def get_cart_details(self, conn):
        """Возвращает детализированную информацию о корзине"""
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

# Глобальный словарь для хранения корзин пользователей
user_carts = {}

def get_user_cart(user_id):
    """Возвращает корзину пользователя, создает новую если не существует"""
    if user_id not in user_carts:
        user_carts[user_id] = Cart(user_id)
    return user_carts[user_id]

# ========== РАБОТА С БАЗОЙ ДАННЫХ ==========
async def create_db_connection():
    """Создает подключение к базе данных"""
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"Ошибка подключения к БД: {e}")
        return None

async def init_db():
    """Инициализация таблиц в базе данных"""
    conn = await create_db_connection()
    if not conn:
        return False

    try:
        # Создаем таблицу товаров
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

        # Создаем таблицу позиций товаров
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

        # Создаем таблицу заказов
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                total_amount DECIMAL(10, 2) NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        # Создаем таблицу позиций заказа
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS order_items (
                id SERIAL PRIMARY KEY,
                order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
                product_id INTEGER REFERENCES products(id),
                item_id INTEGER REFERENCES product_items(id),
                price DECIMAL(10, 2) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')

        logger.info("Таблицы базы данных успешно созданы")
        return True
    except Exception as e:
        logger.error(f"Ошибка создания таблиц: {e}")
        return False
    finally:
        await conn.close()

# ========== ОСНОВНЫЕ КОМАНДЫ ==========
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    """Обработчик команды /start"""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = ["🛍️ Каталог", "🛒 Корзина", "ℹ️ Помощь"]
    
    # Добавляем кнопку админ-панели для администраторов
    if message.from_user.id in ADMIN_IDS:
        buttons.append("⚙️ Админ-панель")
    
    keyboard.add(*buttons)
    
    await message.answer(
        "👋 Добро пожаловать в магазин цифровых товаров!\n"
        "Выберите действие:",
        reply_markup=keyboard
    )

@dp.message_handler(text="ℹ️ Помощь")
async def show_help(message: types.Message):
    """Показывает справку по боту"""
    help_text = (
        "ℹ️ <b>Справка по боту</b>\n\n"
        "🛍️ <b>Каталог</b> - просмотр доступных товаров\n"
        "🛒 <b>Корзина</b> - просмотр и оформление заказа\n"
        "⚙️ <b>Админ-панель</b> - управление товарами (только для админов)\n\n"
        "Для начала работы нажмите /start"
    )
    await message.answer(help_text, parse_mode="HTML")

# ========== АДМИН-ПАНЕЛЬ ==========
@dp.message_handler(text="⚙️ Админ-панель")
async def admin_panel(message: types.Message):
    """Главное меню админ-панели"""
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
    """Возврат в главное меню"""
    await cmd_start(message)

# ========== ДОБАВЛЕНИЕ ТОВАРА ==========
@dp.message_handler(text="📦 Добавить товар")
async def start_adding_product(message: types.Message):
    """Начало процесса добавления товара"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    await AdminStates.waiting_product_name.set()
    await message.answer("Введите название товара:")

@dp.message_handler(state=AdminStates.waiting_product_name)
async def process_product_name(message: types.Message, state: FSMContext):
    """Обработка названия товара"""
    if len(message.text) > 100:
        await message.answer("Название должно быть не длиннее 100 символов")
        return
    
    await state.update_data(name=message.text)
    await AdminStates.waiting_product_description.set()
    await message.answer("Введите описание товара:")

@dp.message_handler(state=AdminStates.waiting_product_description)
async def process_product_description(message: types.Message, state: FSMContext):
    """Обработка описания товара"""
    await state.update_data(description=message.text)
    await AdminStates.waiting_product_price.set()
    await message.answer("Введите цену товара (в рублях):")

@dp.message_handler(state=AdminStates.waiting_product_price)
async def process_product_price(message: types.Message, state: FSMContext):
    """Обработка цены товара"""
    try:
        price = round(float(message.text), 2)
        if price <= 0:
            await message.answer("Цена должна быть больше нуля")
            return
            
        await state.update_data(price=price)
        await AdminStates.waiting_product_photos.set()
        await message.answer("Отправьте фотографии товара:")
    except ValueError:
        await message.answer("Пожалуйста, введите корректную цену (число)")

@dp.message_handler(content_types=types.ContentType.PHOTO, state=AdminStates.waiting_product_photos)
async def process_product_photos(message: types.Message, state: FSMContext):
    """Обработка фотографий товара"""
    photo_ids = [photo.file_id for photo in message.photo]
    await state.update_data(photo_ids=photo_ids)
    
    # Сохраняем товар в базе данных
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
            data['name'], data['description'], data['price'], data['photo_ids']
        )
        
        await state.update_data(product_id=product['id'])
        await AdminStates.waiting_product_items.set()
        
        await message.answer(
            f"✅ Товар успешно добавлен!\n"
            f"Теперь добавьте позиции. Введите локацию и уникальный код через запятую:\n"
            f"Пример: Москва, ABC123"
        )
    except Exception as e:
        logger.error(f"Ошибка добавления товара: {e}")
        await message.answer("Ошибка при добавлении товара")
        await state.finish()
    finally:
        await conn.close()

@dp.message_handler(state=AdminStates.waiting_product_items)
async def process_product_items(message: types.Message, state: FSMContext):
    """Обработка добавления позиций товара"""
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
                f"✅ Добавление товара завершено!\n"
                f"Название: {data.get('name', 'N/A')}\n"
                f"Добавлено позиций: {items_count}"
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
                f"📍 Локация: {location}\n"
                f"🆔 Код: {code}\n\n"
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

# ========== СПИСОК ТОВАРОВ (АДМИН) ==========
@dp.message_handler(text="📝 Список товаров")
async def show_products_list(message: types.Message):
    """Показывает список всех товаров для администратора"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        return
    
    try:
        products = await conn.fetch("SELECT * FROM products ORDER BY id DESC")
        if not products:
            await message.answer("В базе нет товаров")
            return
        
        for product in products:
            # Получаем количество доступных позиций
            available = await conn.fetchval(
                "SELECT COUNT(*) FROM product_items "
                "WHERE product_id = $1 AND is_sold = FALSE",
                product['id']
            )
            
            text = (
                f"🆔 ID: {product['id']}\n"
                f"📦 Название: {product['name']}\n"
                f"💰 Цена: {product['price']} руб.\n"
                f"🛒 Доступно: {available} шт.\n"
                f"📅 Создан: {product['created_at'].strftime('%d.%m.%Y %H:%M')}"
            )
            
            # Отправляем фото, если они есть
            if product['photo_ids']:
                media = [types.InputMediaPhoto(product['photo_ids'][0], caption=text)]
                for photo_id in product['photo_ids'][1:]:
                    media.append(types.InputMediaPhoto(photo_id))
                await bot.send_media_group(message.chat.id, media)
            else:
                await message.answer(text)
                
    except Exception as e:
        logger.error(f"Ошибка получения списка товаров: {e}")
        await message.answer("Ошибка при получении списка товаров")
    finally:
        await conn.close()

# ========== СТАТИСТИКА (АДМИН) ==========
@dp.message_handler(text="📊 Статистика")
async def show_stats(message: types.Message):
    """Показывает статистику магазина"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    conn = await create_db_connection()
    if not conn:
        await message.answer("Ошибка подключения к БД")
        return
    
    try:
        # Получаем статистику
        total_products = await conn.fetchval("SELECT COUNT(*) FROM products")
        total_items = await conn.fetchval("SELECT COUNT(*) FROM product_items")
        sold_items = await conn.fetchval("SELECT COUNT(*) FROM product_items WHERE is_sold = TRUE")
        total_orders = await conn.fetchval("SELECT COUNT(*) FROM orders")
        total_revenue = await conn.fetchval("SELECT COALESCE(SUM(total_amount), 0) FROM orders")
        
        # Формируем сообщение
        stats_text = (
            "📊 <b>Статистика магазина</b>\n\n"
            f"📦 Всего товаров: {total_products}\n"
            f"🛒 Всего позиций: {total_items}\n"
            f"💰 Продано позиций: {sold_items}\n"
            f"🧾 Всего заказов: {total_orders}\n"
            f"💵 Общая выручка: {total_revenue} руб."
        )
        
        await message.answer(stats_text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        await message.answer("Ошибка при получении статистики")
    finally:
        await conn.close()

# ========== КАТАЛОГ ТОВАРОВ ==========
@dp.message_handler(text="🛍️ Каталог")
async def show_catalog(message: types.Message):
    """Показывает каталог товаров"""
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
            # Проверяем наличие товара
            available = await conn.fetchval(
                "SELECT COUNT(*) FROM product_items "
                "WHERE product_id = $1 AND is_sold = FALSE",
                product['id']
            )
            
            if available <= 0:
                continue
            
            btn_text = f"{product['name']} - {product['price']} руб."
            callback_data = f"product_{product['id']}"
            keyboard.add(types.InlineKeyboardButton(
                btn_text, callback_data=callback_data
            ))
        
        await message.answer(
            "🛍️ <b>Каталог товаров</b>\n\n"
            "Выберите товар для просмотра:",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Ошибка показа каталога: {e}")
        await message.answer("Ошибка при загрузке каталога")
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('product_'))
async def show_product_details(callback_query: types.CallbackQuery):
    """Показывает детали товара"""
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
        
        # Проверяем наличие товара
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
        keyboard.add(types.InlineKeyboardButton("🛒 Перейти в корзину", callback_data="view_cart"))
        
        # Отправляем фото, если они есть
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
        logger.error(f"Ошибка показа товара: {e}")
        await callback_query.message.answer("Ошибка при загрузке товара")
    finally:
        await conn.close()

# ========== РАБОТА С КОРЗИНОЙ ==========
@dp.message_handler(text="🛒 Корзина")
async def view_cart(message: types.Message):
    """Показывает содержимое корзины пользователя"""
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
                f"<b>{product['price'] * product['quantity']} руб.</b>\n\n"
            )
        
        text += f"💵 <b>Итого: {total_price} руб.</b>"
        
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("✅ Оформить заказ", callback_data="checkout"),
            types.InlineKeyboardButton("🗑️ Очистить корзину", callback_data="clear_cart")
        )
        
        # Отправляем первое фото из первого товара (если есть) как превью корзины
        first_product = products[0]
        if first_product['photo']:
            await bot.send_photo(
                message.chat.id,
                first_product['photo'],
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
        logger.error(f"Ошибка показа корзины: {e}")
        await message.answer("Ошибка при загрузке корзины")
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('add_to_cart_'))
async def add_to_cart(callback_query: types.CallbackQuery):
    """Добавляет товар в корзину"""
    product_id = int(callback_query.data.split('_')[3])
    cart = get_user_cart(callback_query.from_user.id)
    
    conn = await create_db_connection()
    if not conn:
        await callback_query.message.answer("Ошибка подключения к БД")
        return
    
    try:
        # Проверяем наличие товара
        available = await conn.fetchval(
            "SELECT COUNT(*) FROM product_items "
            "WHERE product_id = $1 AND is_sold = FALSE",
            product_id
        )
        
        if available <= 0:
            await callback_query.answer("Товар закончился")
            return
        
        # Проверяем, не превышает ли количество в корзине доступное количество
        current_in_cart = cart.items.get(product_id, 0)
        if current_in_cart >= available:
            await callback_query.answer("Достигнуто максимальное количество")
            return
        
        cart.add_item(product_id)
        await callback_query.answer(f"Товар добавлен в корзину (всего: {cart.get_total_items()})")
        
    except Exception as e:
        logger.error(f"Ошибка добавления в корзину: {e}")
        await callback_query.answer("Ошибка при добавлении в корзину")
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('remove_from_cart_'))
async def remove_from_cart(callback_query: types.CallbackQuery):
    """Удаляет товар из корзины"""
    product_id = int(callback_query.data.split('_')[3])
    cart = get_user_cart(callback_query.from_user.id)
    
    if cart.remove_item(product_id):
        await callback_query.answer(f"Товар удален из корзины (всего: {cart.get_total_items()})")
    else:
        await callback_query.answer("Этого товара нет в вашей корзине")

@dp.callback_query_handler(lambda c: c.data == 'view_cart')
async def callback_view_cart(callback_query: types.CallbackQuery):
    """Обработчик кнопки просмотра корзины"""
    await callback_query.answer()
    await view_cart(callback_query.message)

@dp.callback_query_handler(lambda c: c.data == 'clear_cart')
async def clear_cart(callback_query: types.CallbackQuery):
    """Очищает корзину пользователя"""
    cart = get_user_cart(callback_query.from_user.id)
    cart.clear()
    await callback_query.answer("Корзина очищена")
    await callback_query.message.edit_text("🛒 Ваша корзина пуста")

# ========== ОФОРМЛЕНИЕ ЗАКАЗА ==========
@dp.callback_query_handler(lambda c: c.data == 'checkout')
async def checkout(callback_query: types.CallbackQuery):
    """Оформление заказа"""
    user_id = callback_query.from_user.id
    cart = get_user_cart(user_id)
    
    if not cart.items:
        await callback_query.answer("Ваша корзина пуста")
        return
    
    conn = await create_db_connection()
    if not conn:
        await callback_query.message.answer("Ошибка подключения к БД")
        return
    
    try:
        # Получаем детали корзины
        products, total_price = await cart.get_cart_details(conn)
        if not products:
            await callback_query.answer("Ваша корзина пуста")
            return
        
        # Проверяем наличие всех товаров
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
        
        # Создаем заказ
        order_id = await conn.fetchval(
            '''INSERT INTO orders (user_id, total_amount, status)
            VALUES ($1, $2, 'pending') RETURNING id''',
            user_id, total_price
        )
        
        # Резервируем товары
        for product in products:
            items = await conn.fetch(
                '''UPDATE product_items
                SET is_sold = TRUE, sold_to = $1, sold_at = NOW()
                WHERE id IN (
                    SELECT id FROM product_items
                    WHERE product_id = $2 AND is_sold = FALSE
                    LIMIT $3
                )
                RETURNING id, unique_code''',
                user_id, product['id'], product['quantity']
            )
            
            # Добавляем товары в заказ
            for item in items:
                await conn.execute(
                    '''INSERT INTO order_items (order_id, product_id, item_id, price)
                    VALUES ($1, $2, $3, $4)''',
                    order_id, product['id'], item['id'], product['price']
                )
        
        # Формируем сообщение о заказе
        text = (
            "✅ <b>Заказ оформлен!</b>\n\n"
            f"🆔 Номер заказа: <code>{order_id}</code>\n"
            f"💵 Сумма: <b>{total_price} руб.</b>\n\n"
            "Спасибо за покупку! Ваши товары:\n\n"
        )
        
        for product in products:
            text += f"📦 {product['name']} × {product['quantity']}\n"
        
        # Очищаем корзину
        cart.clear()
        
        await callback_query.message.edit_text(text, parse_mode="HTML")
        await callback_query.answer()
        
        # Уведомляем администраторов
        admin_text = (
            "🛒 <b>Новый заказ!</b>\n\n"
            f"🆔 Номер: <code>{order_id}</code>\n"
            f"👤 Пользователь: @{callback_query.from_user.username or callback_query.from_user.id}\n"
            f"💵 Сумма: <b>{total_price} руб.</b>"
        )
        
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, admin_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
        
    except Exception as e:
        logger.error(f"Ошибка оформления заказа: {e}")
        await callback_query.message.answer("Произошла ошибка при оформлении заказа")
    finally:
        await conn.close()

# ========== ЗАПУСК БОТА ==========
async def on_startup(dp):
    """Функция, выполняемая при запуске бота"""
    logger.info("Запуск бота...")
    if await init_db():
        logger.info("База данных готова к работе")
    else:
        logger.error("Ошибка инициализации базы данных")
    
    # Уведомление админов о запуске
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, "✅ Бот успешно запущен")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

async def on_shutdown(dp):
    """Функция, выполняемая при остановке бота"""
    logger.info("Остановка бота...")
    # Уведомление админов об остановке
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, "🛑 Бот остановлен")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
    
    # Закрываем соединения
    await dp.storage.close()
    await dp.storage.wait_closed()
    logger.info("Бот остановлен")

if __name__ == '__main__':
    # Запуск бота
    executor.start_polling(
        dp,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True
        )
