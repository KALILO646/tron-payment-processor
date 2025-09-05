# 💰 TRON Payment Processor - USDT Payments for Telegram Bots

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-31%20passed-green.svg)](./test_crypto_module.py)
[![Security](https://img.shields.io/badge/security-hardened-green.svg)](#security)
[![Performance](https://img.shields.io/badge/performance-optimized-blue.svg)](#performance)
[![TRON](https://img.shields.io/badge/TRON-USDT%20TRC20-orange.svg)](https://tron.network/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot%20Ready-blue.svg)](https://telegram.org/)
[![Stars](https://img.shields.io/github/stars/KALILO646/tron-payment-processor?style=social)](https://github.com/KALILO646/tron-payment-processor)
[![Forks](https://img.shields.io/github/forks/KALILO646/tron-payment-processor?style=social)](https://github.com/KALILO646/tron-payment-processor)


> 🚀 **Модуль для приема USDT (TRC20) платежей в Telegram ботах с автоматическим мониторингом, генерацией QR-кодов и защитой от мошенничества**

## ✨ Почему именно этот модуль?

- 🔒 **Максимальная безопасность** - защита от replay-атак, валидация всех данных
- ⚡ **Высокая производительность** - оптимизированные запросы к API, кэширование
- 🎯 **Точность 100%** - уникальные суммы платежей, проверка подтверждений
- 🛡️ **Защита от спама** - rate limiting, лимиты на формы
- 📱 **Готов к использованию** - простой модуль, подробная документация
- 🔧 **Полная настройка** - 50+ параметров конфигурации


## Основные возможности

### Точность проверки
- Генерация уникальных сумм платежей для избежания коллизий
- Проверка подтверждения транзакций в блокчейне TRON
- Валидация адресов кошельков и сумм платежей
- Точное сопоставление платежей с созданными формами

### Защита от злоупотреблений
- Защита от повторного использования транзакций (anti-replay)
- Проверка похожих сумм в недавних транзакциях
- Система истечения платежных форм
- Лимиты на минимальные и максимальные суммы платежей
- Блокировка подозрительных адресов отправителей

### Безопасность
- Rate limiting для предотвращения злоупотреблений API
- Валидация всех входных данных
- Безопасное хранение данных в SQLite
- Подробное логирование всех операций с маскированием чувствительных данных

## Установка

1. Скачайте или клонируйте модуль в ваш проект
```bash
git clone https://github.com/KALILO646/tron-payment-processor
cd tron-payment-processor
```
2. Установите необходимые зависимости:
```bash
pip install -r requirements.txt
```

3. Создайте файл конфигурации `.env` на основе примера:
```bash
cp example.env .env
```

4. Укажите ваш TRON кошелек в `.env`:
```env
WALLET_ADDRESS=TYourWalletAddressHere123456789012345
```

## Быстрый старт

### Базовое использование

```python
from payment_processor import PaymentProcessor
from qr_generator import QRCodeGenerator

# Инициализация процессора платежей
processor = PaymentProcessor()

# Создание платежной формы
payment_form = processor.create_payment_form(
    amount=10.0,
    currency="USDT",
    description="Оплата за товар",
    expires_hours=24
)

print(f"ID формы: {payment_form['form_id']}")
print(f"Сумма к оплате: {payment_form['amount']} {payment_form['currency']}")
print(f"Адрес для оплаты: {payment_form['wallet_address']}")

# Генерация QR-кода для оплаты
qr_generator = QRCodeGenerator()
qr_data = processor.generate_payment_qr_data(payment_form['form_id'])
qr_generator.generate_qr_code_file(qr_data, f"payment_{payment_form['form_id'][:8]}.png")

# Запуск мониторинга платежей
def on_payment_received(transaction, form_id):
    print(f"Получен платеж для формы {form_id}")
    print(f"Сумма: {transaction['amount']} {transaction['currency']}")
    print(f"От: {transaction['from_address']}")

processor.register_payment_callback(payment_form['form_id'], on_payment_received)
processor.start_monitoring()

# Проверка статуса платежа
status = processor.check_payment_status(payment_form['form_id'])
print(f"Статус платежа: {status['status']}")
```

### Интеграция с Telegram ботом

```python
import telebot
from payment_processor import PaymentProcessor

bot = telebot.TeleBot("YOUR_BOT_TOKEN")
processor = PaymentProcessor()

@bot.message_handler(commands=['pay'])
def handle_payment(message):
    try:
        # Создаем платежную форму
        payment_form = processor.create_payment_form(
            amount=100.0,
            currency="USDT",
            description=f"Платеж от пользователя {message.from_user.id}",
            client_ip=None,  # Можно передать IP пользователя для лимитов
            user_id=str(message.from_user.id)
        )
        
        # Генерируем QR-код
        qr_data = processor.generate_payment_qr_data(payment_form['form_id'])
        qr_filename = f"payment_{payment_form['form_id'][:8]}.png"
        
        from qr_generator import QRCodeGenerator
        qr_gen = QRCodeGenerator()
        qr_gen.generate_qr_code_file(qr_data, qr_filename)
        
        # Отправляем информацию о платеже
        bot.send_message(message.chat.id, 
            f"Для оплаты переведите {payment_form['amount']} USDT на адрес:\n"
            f"`{payment_form['wallet_address']}`\n\n"
            f"ID платежа: `{payment_form['form_id']}`\n"
            f"Срок действия: {payment_form['expires_at']}", 
            parse_mode='Markdown')
        
        # Отправляем QR-код
        with open(qr_filename, 'rb') as photo:
            bot.send_photo(message.chat.id, photo, 
                caption="QR-код для быстрой оплаты")
        
        # Регистрируем callback для уведомления об оплате
        def payment_callback(transaction, form_id):
            bot.send_message(message.chat.id, 
                f"Платеж получен! Сумма: {transaction['amount']} USDT")
        
        processor.register_payment_callback(payment_form['form_id'], payment_callback)
        
    except Exception as e:
        bot.send_message(message.chat.id, f"Ошибка создания платежа: {str(e)}")

# Запускаем мониторинг платежей
processor.start_monitoring()
bot.polling()
```

## Конфигурация

Создайте файл `.env` с необходимыми настройками:

```env
# Конфигурация TronScan API
TRONSCAN_API_URL=https://apilist.tronscanapi.com/api
WALLET_ADDRESS=

# Конфигурация Telegram бота
TELEGRAM_BOT_TOKEN=

# Конфигурация базы данных
DATABASE_PATH=transaction.db

# Конфигурация мониторинга
CHECK_INTERVAL=60

UNIQUE_AMOUNT_CHECK_HOURS=2

API_RATE_LIMIT=20

LOG_LEVEL=INFO

MAX_USDT_AMOUNT=10000.0
MAX_TRX_AMOUNT=100000.0
MIN_USDT_AMOUNT=0.1
MIN_TRX_AMOUNT=1.0

MONITOR_INTERVAL=30

MAX_FORM_LIFETIME=2

BLACKLISTED_ADDRESSES=

MIN_CONFIRMATIONS_USDT=19
MIN_CONFIRMATIONS_TRX=19

# Дополнительные переменные конфигурации
# ======================================

# Конфигурация API
API_REQUESTS_PER_MINUTE=20
API_CACHE_TTL_SECONDS=30

# Конфигурация базы данных
DB_POOL_SIZE=5
DB_CONNECTION_TIMEOUT=30.0
DB_POOL_TIMEOUT=10.0
DB_CACHE_SIZE=10000
DB_MMAP_SIZE=268435456

# Конфигурация платежных форм
DEFAULT_FORM_EXPIRES_HOURS=24
MAX_TOTAL_FORMS=1000
MIN_FORM_CREATION_INTERVAL_SECONDS=0.5
MIN_USER_FORM_INTERVAL_SECONDS=2.0
MAX_USER_FORMS_PER_HOUR=20
USER_COUNTERS_CLEANUP_HOURS=1

# Валидация транзакций
MAX_TRANSACTION_AGE_HOURS=2
FUTURE_TOLERANCE_MINUTES=5
DEFAULT_MIN_CONFIRMATIONS=19

# Валидация данных
MAX_DESCRIPTION_LENGTH=500
MAX_AMOUNT_LIMIT=1000000000000000.0

# Кэширование
CACHE_EXPIRY_SECONDS=300

# Конфигурация QR-кодов
QR_CODES_DIR=qr_codes
QR_VERSION=1
QR_ERROR_CORRECTION=L
QR_BOX_SIZE=10
QR_BORDER=4
QR_DEFAULT_SIZE=300
QR_FILL_COLOR=black
QR_BACK_COLOR=white
MAX_FILENAME_LENGTH=255
MAX_QR_DATA_LENGTH=2000

```

## API методы

### PaymentProcessor

#### create_payment_form(amount, currency, description, expires_hours, client_ip, user_id)
Создание новой платежной формы с защитой от злоупотреблений.

**Параметры:**
- `amount` (float) - сумма платежа
- `currency` (str) - валюта ("USDT" или "TRX")
- `description` (str) - описание платежа
- `expires_hours` (int) - время жизни формы в часах
- `client_ip` (str, опционально) - IP адрес клиента для лимитов
- `user_id` (str, опционально) - ID пользователя для лимитов

**Возвращает:** словарь с данными формы

#### start_monitoring(check_interval)
Запуск мониторинга входящих платежей.

#### stop_monitoring()
Остановка мониторинга платежей.

#### check_payment_status(form_id)
Проверка статуса конкретного платежа.

#### register_payment_callback(form_id, callback)
Регистрация функции обратного вызова для уведомления о платеже.

#### generate_payment_qr_data(form_id)
Генерация данных для QR-кода платежа.

#### get_payment_form(form_id)
Получение информации о платежной форме.

### QRCodeGenerator

#### generate_qr_code(data, size)
Генерация QR-кода в виде изображения.

#### generate_qr_code_file(data, filename, size)
Сохранение QR-кода в файл.


### Проверка статуса компонентов

```python
# Проверка соединения с TronScan API
account_info = processor.tronscan.get_account_info(processor.wallet_address)
print(f"Информация о кошельке: {account_info}")

# Проверка базы данных
active_forms = processor.db.get_active_payment_forms(time.time())
print(f"Активных форм: {len(active_forms)}")

# Проверка последних транзакций
transactions = processor.tronscan.get_account_transactions(processor.wallet_address, limit=10)
print(f"Последних транзакций: {len(transactions)}")
```

## Лицензия

Этот проект распространяется под MIT лицензией. Вы можете свободно использовать, изменять и распространять код с сохранением указания авторства.
