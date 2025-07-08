import logging
import os
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.utils import executor
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

# Инициализация бота и диспетчера
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

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
    waiting_for_product_to_delete = State()

# Подключение к базе данных
async def create_db_connection():
    return await asyncpg.connect(DATABASE_URL)

async def init_db():
    conn = await create_db_connection()
    try:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                price INTEGER NOT NULL,
                description TEXT,
                photo_id VARCHAR(200),
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                contact_info TEXT NOT NULL,
                address TEXT NOT NULL,
                total_price INTEGER NOT NULL,
                status VARCHAR(20) DEFAULT 'new',
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS order_items (
                id SERIAL PRIMARY KEY,
                order_id INTEGER REFERENCES orders(id),
                product_id INTEGER REFERENCES products(id),
                quantity INTEGER NOT NULL,
                price INTEGER NOT NULL
            )
        ''')
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
        await message.answer("Доступ запрещен!")
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
async def add_product_step1(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещен!")
        return
    
    await AdminStates.waiting_for_product_name.set()
    await message.answer("Введите название товара:")

# Добавление товара - шаг 2 (цена)
@dp.message_handler(state=AdminStates.waiting_for_product_name)
async def add_product_step2(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await AdminStates.waiting_for_product_price.set()
    await message.answer("Введите цену товара (в рублях):")

# Добавление товара - шаг 3 (описание)
@dp.message_handler(state=AdminStates.waiting_for_product_price)
async def add_product_step3(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите число!")
        return
    
    await state.update_data(price=int(message.text))
    await AdminStates.waiting_for_product_description.set()
    await message.answer("Введите описание товара:")

# Добавление товара - шаг 4 (фото)
@dp.message_handler(state=AdminStates.waiting_for_product_description)
async def add_product_step4(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await AdminStates.waiting_for_product_photo.set()
    await message.answer("Отправьте фото товара:")

# Добавление товара - завершение
@dp.message_handler(state=AdminStates.waiting_for_product_photo, content_types=types.ContentType.PHOTO)
async def add_product_final(message: types.Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    user_data = await state.get_data()
    
    conn = await create_db_connection()
    try:
        await conn.execute(
            '''
            INSERT INTO products (name, price, description, photo_id)
            VALUES ($1, $2, $3, $4)
            ''',
            user_data['name'], user_data['price'], user_data['description'], photo_id
        )
        await message.answer("✅ Товар успешно добавлен!")
    except Exception as e:
        logger.error(f"Ошибка при добавлении товара: {e}")
        await message.answer(f"❌ Ошибка при добавлении товара: {e}")
    finally:
        await conn.close()
        await state.finish()

# Удаление товара
@dp.message_handler(text="🗑️ Удалить товар")
async def delete_product_start(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещен!")
        return
    
    conn = await create_db_connection()
    try:
        products = await conn.fetch("SELECT * FROM products ORDER BY id")
        
        if not products:
            await message.answer("Товаров пока нет.")
            return
        
        keyboard = types.InlineKeyboardMarkup()
        
        for product in products:
            keyboard.add(
                types.InlineKeyboardButton(
                    f"❌ {product['id']}. {product['name']} - {product['price']} руб.",
                    callback_data=f"delete_{product['id']}"
                )
            )
        
        await message.answer("Выберите товар для удаления:", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Ошибка при получении списка товаров: {e}")
        await message.answer("❌ Ошибка при получении списка товаров")
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
        
        await bot.answer_callback_query(callback_query.id, "✅ Товар удален!")
        await bot.send_message(
            callback_query.from_user.id,
            f"🗑️ Товар успешно удален:\n"
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

# Показать список товаров (админ)
@dp.message_handler(text="📝 Список товаров")
async def show_products_admin(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещен!")
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
async def add_to_cart(message: types.Message):
    product_id = int(message.text)
    
    conn = await create_db_connection()
    try:
        product = await conn.fetchrow("SELECT * FROM products WHERE id = $1", product_id)
        
        if not product:
            await message.answer("❌ Товар не найден!")
            return
        
        await UserStates.waiting_for_quantity.set()
        state = dp.current_state(user=message.from_user.id)
        await state.update_data(product_id=product_id)
        
        response = (
            f"Вы выбрали: {product['name']}\n"
            f"Цена: {product['price']} руб.\n"
            f"Введите количество:"
        )
        
        if product['photo_id']:
            await bot.send_photo(
                chat_id=message.chat.id,
                photo=product['photo_id'],
                caption=response
            )
        else:
            await message.answer(response)
    except Exception as e:
        logger.error(f"Ошибка при добавлении в корзину: {e}")
        await message.answer("❌ Ошибка при обработке товара")
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
        await message.answer("Доступ запрещен!")
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
    logging.info("Бот успешно запущен")

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
