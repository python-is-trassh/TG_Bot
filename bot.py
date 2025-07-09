import logging
import os
from aiogram import Bot, Dispatcher, types, executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
import asyncpg
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
API_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id]
DATABASE_URL = os.getenv('DATABASE_URL')

# Инициализация бота
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Состояния для FSM
class UserStates(StatesGroup):
    waiting_for_quantity = State()
    waiting_for_contact = State()
    waiting_for_address = State()

class AdminStates(StatesGroup):
    waiting_for_product_name = State()
    waiting_for_product_price = State()
    waiting_for_product_description = State()
    waiting_for_product_photo = State()

# Подключение к базе данных
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Таблица товаров (основные характеристики)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                description TEXT,
                price DECIMAL(10, 2) NOT NULL,
                photo_ids TEXT[],  # Массив photo_id
                created_at TIMESTAMP DEFAULT NOW(),
                is_active BOOLEAN DEFAULT TRUE
            )
        ''')
        
        # Таблица позиций (конкретные экземпляры товара)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS product_items (
                id SERIAL PRIMARY KEY,
                product_id INTEGER REFERENCES products(id),
                location VARCHAR(100) NOT NULL,
                unique_code VARCHAR(50) UNIQUE,
                is_sold BOOLEAN DEFAULT FALSE,
                sold_at TIMESTAMP,
                sold_to INTEGER  # user_id покупателя
            )
        ''')
        
        # Таблица корзины
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS carts (
                user_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        
        # Элементы корзины
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS cart_items (
                id SERIAL PRIMARY KEY,
                cart_user_id BIGINT REFERENCES carts(user_id),
                product_item_id INTEGER REFERENCES product_items(id),
                quantity INTEGER DEFAULT 1,
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
                completed_at TIMESTAMP
            )
        ''')
        
        # Позиции заказа
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS order_items (
                id SERIAL PRIMARY KEY,
                order_id INTEGER REFERENCES orders(id),
                product_item_id INTEGER REFERENCES product_items(id),
                quantity INTEGER NOT NULL,
                price DECIMAL(10, 2) NOT NULL,
                location VARCHAR(100) NOT NULL
            )
        ''' 
        logger.info("База данных успешно инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
    finally:
        await conn.close()

# Команда /start
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    
    if message.from_user.id in ADMIN_IDS:
        buttons = ["🛍️ Каталог", "🛒 Корзина", "ℹ️ О нас", "📞 Контакты", "⚙️ Админ-панель"]
    else:
        buttons = ["🛍️ Каталог", "🛒 Корзина", "ℹ️ О нас", "📞 Контакты"]
    
    keyboard.add(*buttons)
    
    await message.answer(
        "Добро пожаловать в наш интернет-магазин!\n"
        "Выберите действие из меню ниже:",
        reply_markup=keyboard
    )

# Админ-панель
@dp.message_handler(text="⚙️ Админ-панель")
async def admin_panel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещён!")
        return
    
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = ["📦 Добавить товар", "🗑️ Удалить товар", "📝 Список товаров", "📊 Статистика", "🔙 Назад"]
    keyboard.add(*buttons)
    await message.answer("Админ-панель:", reply_markup=keyboard)

# Назад в главное меню
@dp.message_handler(text="🔙 Назад")
async def back_to_main(message: types.Message):
    await cmd_start(message)

# Добавление товара - шаг 1 (название)
@dp.message_handler(text="📦 Добавить товар")
class AddProductStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_photos = State()
    waiting_for_items = State()

@dp.message_handler(commands=['add_product'])
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
    await message.answer("Введите цену товара:")

@dp.message_handler(state=AddProductStates.waiting_for_price)
async def process_product_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text)
        await state.update_data(price=price)
        await AddProductStates.waiting_for_photos.set()
        await message.answer("Отправьте фотографии товара (несколько фото можно отправить как альбом):")
    except ValueError:
        await message.answer("Пожалуйста, введите корректную цену (число):")

@dp.message_handler(state=AddProductStates.waiting_for_photos, content_types=types.ContentType.PHOTO)
async def process_product_photos(message: types.Message, state: FSMContext):
    photo_ids = [photo.file_id for photo in message.photo]
    await state.update_data(photo_ids=photo_ids)
    
    data = await state.get_data()
    
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        product = await conn.fetchrow(
            'INSERT INTO products (name, description, price, photo_ids) '
            'VALUES ($1, $2, $3, $4) RETURNING *',
            data['name'], data['description'], data['price'], data['photo_ids']
        )
        
        await state.update_data(product_id=product['id'])
        await AddProductStates.waiting_for_items.set()
        
        await message.answer(
            f"✅ Товар {product['name']} создан. Теперь добавьте позиции.\n"
            "Введите локацию и уникальный код через запятую (например: Москва, ABC123):"
        )
    finally:
        await conn.close()

@dp.message_handler(state=AddProductStates.waiting_for_items)
async def process_product_items(message: types.Message, state: FSMContext):
    try:
        location, unique_code = map(str.strip, message.text.split(','))
        data = await state.get_data()
        
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            await conn.execute(
                'INSERT INTO product_items (product_id, location, unique_code) '
                'VALUES ($1, $2, $3)',
                data['product_id'], location, unique_code
            )
            
            await message.answer(
                f"✅ Позиция добавлена: {location} - {unique_code}\n"
                "Отправьте следующую пару локация,код или /done для завершения"
            )
        finally:
            await conn.close()
    except Exception as e:
        await message.answer(
            "Неверный формат. Введите локацию и уникальный код через запятую:\n"
            "Например: Москва, ABC123"
        )

@dp.message_handler(commands=['done'], state=AddProductStates.waiting_for_items)
async def finish_adding_items(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.finish()
    
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        items_count = await conn.fetchval(
            'SELECT COUNT(*) FROM product_items WHERE product_id = $1',
            data['product_id']
        )
        
        await message.answer(
            f"✅ Добавление товара завершено!\n"
            f"Название: {data['name']}\n"
            f"Позиций: {items_count}\n"
            f"Теперь товар доступен в каталоге."
        )
    finally:
        await conn.close()

# Обработчик удаления товара
@dp.callback_query_handler(lambda c: c.data.startswith('delete_'))
async def process_delete_product(callback_query: types.CallbackQuery):
    product_id = int(callback_query.data.split('_')[1])
    
    conn = await create_db_connection()
    try:
        product = await conn.fetchrow("SELECT * FROM products WHERE id = $1", product_id)
        
        if not product:
            await bot.answer_callback_query(callback_query.id, "❌ Товар не найден!")
            return
        
        # Удаляем связанные записи в order_items сначала
        await conn.execute("DELETE FROM order_items WHERE product_id = $1", product_id)
        # Затем удаляем сам товар
        await conn.execute("DELETE FROM products WHERE id = $1", product_id)
        
        await bot.answer_callback_query(callback_query.id, "✅ Товар удалён!")
        await bot.send_message(
            callback_query.from_user.id,
            f"🗑️ Товар успешно удалён:\n"
            f"ID: {product['id']}\n"
            f"Название: {product['name']}\n"
            f"Цена: {product['price']} руб."
        )
        
        # Обновляем список товаров
        await delete_product_start(callback_query.message)
    except Exception as e:
        logger.error(f"Ошибка при удалении товара: {e}")
        await bot.answer_callback_query(callback_query.id, f"❌ Ошибка: {str(e)}")
    finally:
        await conn.close()

#каталог
@dp.message_handler(text="🛍️ Каталог")
async def show_catalog(message: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        products = await conn.fetch(
            'SELECT * FROM products WHERE is_active = TRUE ORDER BY id'
        )
        
        if not products:
            await message.answer("Каталог пуст")
            return
        
        for product in products:
            available_items = await conn.fetchval(
                'SELECT COUNT(*) FROM product_items '
                'WHERE product_id = $1 AND is_sold = FALSE',
                product['id']
            )
            
            text = (
                f"📦 *{product['name']}*\n"
                f"💰 Цена: {product['price']} руб.\n"
                f"🛒 Доступно: {available_items} шт.\n"
                f"📝 {product['description']}"
            )
            
            # Отправляем первую фотографию как основную
            if product['photo_ids']:
                media = [types.InputMediaPhoto(product['photo_ids'][0], caption=text)]
                # Добавляем остальные фото в медиагруппу
                for photo_id in product['photo_ids'][1:]:
                    media.append(types.InputMediaPhoto(photo_id))
                
                await bot.send_media_group(message.chat.id, media)
            
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton(
                "📍 Выбрать локацию", 
                callback_data=f"select_location_{product['id']}"
            ))
            
            await message.answer(
                "Выберите локацию для покупки:",
                reply_markup=keyboard
            )
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('select_location_'))
async def select_location(callback_query: types.CallbackQuery):
    product_id = int(callback_query.data.split('_')[2])
    
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        locations = await conn.fetch(
            'SELECT DISTINCT location FROM product_items '
            'WHERE product_id = $1 AND is_sold = FALSE',
            product_id
        )
        
        if not locations:
            await callback_query.answer("Нет доступных позиций")
            return
        
        keyboard = InlineKeyboardMarkup(row_width=2)
        for loc in locations:
            keyboard.add(InlineKeyboardButton(
                loc['location'], 
                callback_data=f"show_items_{product_id}_{loc['location']}"
            ))
        
        await callback_query.message.edit_text(
            "Выберите локацию:",
            reply_markup=keyboard
        )
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('show_items_'))
async def show_available_items(callback_query: types.CallbackQuery):
    _, _, product_id, location = callback_query.data.split('_')
    product_id = int(product_id)
    
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        items = await conn.fetch(
            'SELECT id, unique_code FROM product_items '
            'WHERE product_id = $1 AND location = $2 AND is_sold = FALSE',
            product_id, location
        )
        
        if not items:
            await callback_query.answer("Нет доступных позиций")
            return
        
        product = await conn.fetchrow(
            'SELECT name, price FROM products WHERE id = $1', 
            product_id
        )
        
        text = (
            f"📍 *{location}*\n"
            f"📦 {product['name']} - {product['price']} руб.\n"
            f"Доступные позиции:"
        )
        
        keyboard = InlineKeyboardMarkup()
        for item in items:
            keyboard.add(InlineKeyboardButton(
                f"🛒 {item['unique_code']}", 
                callback_data=f"add_to_cart_{item['id']}"
            ))
        
        await callback_query.message.edit_text(
            text,
            reply_markup=keyboard
        )
    finally:
        await conn.close()

@dp.callback_query_handler(lambda c: c.data.startswith('add_to_cart_'))
async def add_item_to_cart(callback_query: types.CallbackQuery):
    item_id = int(callback_query.data.split('_')[3])
    
    success = await add_to_cart(callback_query.from_user.id, item_id)
    if success:
        await callback_query.answer("Товар добавлен в корзину!")
    else:
        await callback_query.answer("Товар уже продан или недоступен")
```

# Показать список товаров (админ)
@dp.message_handler(text="📝 Список товаров")
async def show_products_admin(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещён!")
        return
    
    conn = await create_db_connection()
    try:
        products = await conn.fetch("SELECT * FROM products ORDER BY id")
        
        if not products:
            await message.answer("Товаров пока нет.")
            return
        
        for product in products:
            caption = (
                f"🆔 ID: {product['id']}\n"
                f"📛 Название: {product['name']}\n"
                f"💰 Цена: {product['price']} руб.\n"
                f"📝 Описание: {product['description']}\n"
                f"📅 Добавлен: {product['created_at'].strftime('%d.%m.%Y %H:%M')}"
            )
            
            if product['photo_id']:
                await bot.send_photo(
                    chat_id=message.chat.id,
                    photo=product['photo_id'],
                    caption=caption
                )
            else:
                await message.answer(caption)
    except Exception as e:
        logger.error(f"Ошибка при получении списка товаров: {e}")
        await message.answer("❌ Ошибка при получении списка товаров")
    finally:
        await conn.close()

# Показать каталог товаров (пользователь)
@dp.message_handler(text="🛍️ Каталог")
async def show_catalog(message: types.Message):
    conn = await create_db_connection()
    try:
        products = await conn.fetch("SELECT * FROM products ORDER BY id")
        
        if not products:
            await message.answer("Товаров пока нет.")
            return
        
        for product in products:
            caption = (
                f"📛 {product['name']}\n"
                f"💰 Цена: {product['price']} руб.\n"
                f"📝 {product['description']}\n\n"
                f"Введите ID товара ({product['id']}) чтобы добавить в корзину"
            )
            
            if product['photo_id']:
                await bot.send_photo(
                    chat_id=message.chat.id,
                    photo=product['photo_id'],
                    caption=caption
                )
            else:
                await message.answer(caption)
    except Exception as e:
        logger.error(f"Ошибка при получении каталога: {e}")
        await message.answer("❌ Ошибка при загрузке каталога")
    finally:
        await conn.close()

# Добавление товара в корзину
@dp.message_handler(lambda message: message.text.isdigit())
async def get_or_create_cart(user_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        cart = await conn.fetchrow(
            'SELECT * FROM carts WHERE user_id = $1', user_id
        )
        if not cart:
            cart = await conn.fetchrow(
                'INSERT INTO carts (user_id) VALUES ($1) RETURNING *', user_id
            )
        return cart
    finally:
        await conn.close()

async def add_to_cart(user_id: int, product_item_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Проверяем доступность позиции
        item = await conn.fetchrow(
            'SELECT * FROM product_items WHERE id = $1 AND is_sold = FALSE', 
            product_item_id
        )
        if not item:
            return False
        
        # Добавляем в корзину
        await conn.execute('''
            INSERT INTO cart_items (cart_user_id, product_item_id)
            VALUES ($1, $2)
            ON CONFLICT (cart_user_id, product_item_id) 
            DO UPDATE SET quantity = cart_items.quantity + 1
        ''', user_id, product_item_id)
        
        # Обновляем время изменения корзины
        await conn.execute(
            'UPDATE carts SET updated_at = NOW() WHERE user_id = $1', 
            user_id
        )
        return True
    finally:
        await conn.close()

async def get_cart_contents(user_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        return await conn.fetch('''
            SELECT 
                ci.id as cart_item_id,
                pi.id as product_item_id,
                p.name,
                p.description,
                p.price,
                pi.location,
                ci.quantity
            FROM cart_items ci
            JOIN product_items pi ON ci.product_item_id = pi.id
            JOIN products p ON pi.product_id = p.id
            WHERE ci.cart_user_id = $1
            ORDER BY ci.added_at DESC
        ''', user_id)
    finally:
        await conn.close()

# ========== ХЕНДЛЕРЫ КОРЗИНЫ ==========

@dp.message_handler(text="🛒 Корзина")
async def show_cart(message: types.Message):
    cart_items = await get_cart_contents(message.from_user.id)
    
    if not cart_items:
        await message.answer("Ваша корзина пуста")
        return
    
    total = sum(item['price'] * item['quantity'] for item in cart_items)
    text = "🛒 *Ваша корзина*\n\n"
    
    for item in cart_items:
        text += (
            f"📌 *{item['name']}*\n"
            f"📍 Локация: {item['location']}\n"
            f"💰 Цена: {item['price']} руб. × {item['quantity']} = "
            f"{item['price'] * item['quantity']} руб.\n"
            f"└── [Удалить](tg://btn/{item['cart_item_id']})\n\n"
        )
    
    text += f"💵 *Итого: {total} руб.*"
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("💳 Оформить заказ", callback_data="checkout"))
    
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query_handler(lambda c: c.data == "checkout")
async def process_checkout(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    cart_items = await get_cart_contents(user_id)
    
    if not cart_items:
        await callback_query.answer("Корзина пуста!")
        return
    
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Создаем заказ
        total = sum(item['price'] * item['quantity'] for item in cart_items)
        order = await conn.fetchrow(
            'INSERT INTO orders (user_id, total_amount) VALUES ($1, $2) RETURNING *',
            user_id, total
        )
        
        # Добавляем позиции заказа
        for item in cart_items:
            await conn.execute('''
                INSERT INTO order_items 
                (order_id, product_item_id, quantity, price, location)
                VALUES ($1, $2, $3, $4, $5)
            ''', order['id'], item['product_item_id'], item['quantity'], 
            item['price'], item['location'])
            
            # Помечаем позиции как проданные
            await conn.execute(
                'UPDATE product_items SET is_sold = TRUE, sold_at = NOW(), '
                'sold_to = $1 WHERE id = $2',
                user_id, item['product_item_id']
            )
        
        # Очищаем корзину
        await conn.execute(
            'DELETE FROM cart_items WHERE cart_user_id = $1', 
            user_id
        )
        
        await callback_query.message.edit_text(
            "✅ Заказ успешно оформлен! Номер вашего заказа: #" + str(order['id']),
            reply_markup=None
        )
        
        # Уведомление администратору
        for admin_id in ADMIN_IDS:
            await bot.send_message(
                admin_id,
                f"📦 Новый заказ #{order['id']}\n"
                f"👤 Пользователь: {callback_query.from_user.full_name}\n"
                f"💳 Сумма: {total} руб.\n"
                f"🛒 Товаров: {len(cart_items)}"
            )
    finally:
        await conn.close()
        
# Обработка количества товара
@dp.message_handler(state=UserStates.waiting_for_quantity)
async def process_quantity(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите число!")
        return
    
    quantity = int(message.text)
    if quantity <= 0:
        await message.answer("Количество должно быть больше нуля!")
        return
    
    user_data = await state.get_data()
    product_id = user_data['product_id']
    
    conn = await create_db_connection()
    try:
        product = await conn.fetchrow("SELECT * FROM products WHERE id = $1", product_id)
        
        await message.answer(
            f"✅ Добавлено {quantity} шт. {product['name']} в корзину!\n"
            f"💰 Общая стоимость: {product['price'] * quantity} руб."
        )
    except Exception as e:
        logger.error(f"Ошибка при обработке количества: {e}")
        await message.answer("❌ Ошибка при добавлении товара")
    finally:
        await conn.close()
        await state.finish()

# Просмотр корзины
@dp.message_handler(text="🛒 Корзина")
async def show_cart(message: types.Message):
    # Здесь должна быть реализация работы с корзиной
    await message.answer("🛒 Функционал корзины будет реализован в следующей версии")

# Статистика для админа
@dp.message_handler(text="📊 Статистика")
async def show_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещён!")
        return
    
    conn = await create_db_connection()
    try:
        total_products = await conn.fetchval("SELECT COUNT(*) FROM products")
        total_orders = await conn.fetchval("SELECT COUNT(*) FROM orders")
        total_revenue = await conn.fetchval("SELECT COALESCE(SUM(total_price), 0) FROM orders")
        last_products = await conn.fetch("SELECT * FROM products ORDER BY created_at DESC LIMIT 5")
        
        stats_text = (
            "📊 Статистика магазина:\n\n"
            f"📦 Товаров в каталоге: {total_products}\n"
            f"📝 Всего заказов: {total_orders}\n"
            f"💰 Общая выручка: {total_revenue} руб.\n\n"
            "Последние добавленные товары:\n"
        )
        
        for product in last_products:
            stats_text += f"- {product['name']} ({product['price']} руб.)\n"
        
        await message.answer(stats_text)
    except Exception as e:
        logger.error(f"Ошибка при получении статистики: {e}")
        await message.answer("❌ Ошибка при загрузке статистики")
    finally:
        await conn.close()

# Информация о магазине
@dp.message_handler(text="ℹ️ О нас")
async def about_us(message: types.Message):
    await message.answer(
        "🏪 Мы - лучший интернет-магазин электроники!\n"
        "🛠️ Работаем с 2020 года. Гарантия качества!\n"
        "🚚 Быстрая доставка по всей стране"
    )

# Контакты
@dp.message_handler(text="📞 Контакты")
async def contacts(message: types.Message):
    await message.answer(
        "📞 Наши контакты:\n\n"
        "☎️ Телефон: +7 (123) 456-78-90\n"
        "📧 Email: info@example.com\n"
        "🏠 Адрес: г. Москва, ул. Примерная, д. 1\n\n"
        "⏰ Режим работы: Пн-Пт 9:00-18:00"
    )

async def on_startup(dp):
    await init_db()
    logger.info("Бот успешно запущен")

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
