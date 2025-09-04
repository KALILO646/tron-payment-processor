import os
import uuid
import threading
import time
import random
import secrets
import logging
import hashlib
import re
import functools
import ipaddress
from datetime import datetime, timedelta
from typing import Dict, Optional, Callable, List, Any
from dotenv import load_dotenv

from database import DatabaseManager
from tronscan_api import TronScanAPI

load_dotenv()

def retry_on_failure(max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0, 
                    exceptions: tuple = (Exception,)):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay
            
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    
                    if attempt == max_retries:
                        break
                    
                    if args and hasattr(args[0], 'logger'):
                        args[0].logger.warning(
                            f"Попытка {attempt + 1}/{max_retries + 1} не удалась для {func.__name__}: {e}. "
                            f"Повтор через {current_delay:.1f} секунд"
                        )
                    
                    time.sleep(current_delay)
                    current_delay *= backoff
                    
            raise last_exception
        return wrapper
    return decorator

class PaymentProcessor:
    def __init__(self, log_level: str = "INFO"):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(getattr(logging, log_level.upper()))
        
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        
        try:
            self._validate_env_vars()
            
            self.db = DatabaseManager(os.getenv('DATABASE_PATH', 'transaction.db'))
            self.tronscan = TronScanAPI(os.getenv('TRONSCAN_API_URL', 'https://apilist.tronscanapi.com/api'))
            self.wallet_address = os.getenv('WALLET_ADDRESS')
            
            if not self.wallet_address:
                raise ValueError("WALLET_ADDRESS не указан в .env файле")
            
            self.monitoring = False
            self.monitor_thread = None
            self.payment_callbacks = {}
            
            self.logger.info(f"PaymentProcessor инициализирован для кошелька: {self._mask_wallet_address(self.wallet_address)}")
            
        except Exception as e:
            self.logger.error(f"Ошибка инициализации PaymentProcessor: {e}")
            raise
    
    def _mask_wallet_address(self, address: str) -> str:
        if not address or len(address) < 8:
            return "****"
        return f"{address[:4]}...{address[-4:]}"
    
    def _mask_amount(self, amount: float) -> str:
        return "***.**"
    
    def _validate_env_vars(self):
        wallet = os.getenv('WALLET_ADDRESS')
        if not wallet or not self._validate_tron_address(wallet):
            raise ValueError("Некорректный WALLET_ADDRESS в переменных окружения")
        
        api_url = os.getenv('TRONSCAN_API_URL', 'https://apilist.tronscanapi.com/api')
        if not api_url.startswith(('http://', 'https://')):
            raise ValueError("Некорректный TRONSCAN_API_URL в переменных окружения")
        
        try:
            rate_limit = int(os.getenv('API_RATE_LIMIT', 60))
            if rate_limit < 1 or rate_limit > 1000:
                raise ValueError("API_RATE_LIMIT должен быть от 1 до 1000")
        except ValueError:
            raise ValueError("Некорректный API_RATE_LIMIT в переменных окружения")
    
    def _validate_description(self, description: str) -> bool:
        if not isinstance(description, str):
            return False
            
        if len(description) > 500:
            return False
        
        dangerous_chars = ['<', '>', '"', "'", '&', '\n', '\r', '\t', '\0', '\x1a']
        if any(char in description for char in dangerous_chars):
            return False
        
        sql_keywords = ['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'DROP', 'CREATE', 
                       'ALTER', 'EXEC', 'UNION', 'SCRIPT', 'JAVASCRIPT']
        description_upper = description.upper()
        if any(keyword in description_upper for keyword in sql_keywords):
            return False
        
        if any(ord(char) < 32 and char not in [' ', '\t'] for char in description):
            return False
            
        return True
    
    def _validate_tron_address(self, address: str) -> bool:
        if not address:
            return False
        
        if not re.match(r'^T[A-Za-z0-9]{33}$', address):
            return False
        
        return True
    
    def _validate_amount(self, amount: float, currency: str) -> bool:
        if not isinstance(amount, (int, float)):
            return False
            
        if not isinstance(currency, str):
            return False
        
        try:
            if not (amount == amount):
                return False
        except (TypeError, ValueError):
            return False
            
        if amount == float('inf') or amount == float('-inf'):
            return False
        
        if amount <= 0:
            return False
        
        if amount != round(amount, 6):
            self.logger.warning(f"Сумма имеет слишком высокую точность: {amount}")
            return False
        
        min_limits = {
            'USDT': float(os.getenv('MIN_USDT_AMOUNT', 0.1)),
            'TRX': float(os.getenv('MIN_TRX_AMOUNT', 1.0))
        }
        
        max_limits = {
            'USDT': float(os.getenv('MAX_USDT_AMOUNT', 10000.0)),
            'TRX': float(os.getenv('MAX_TRX_AMOUNT', 100000.0))
        }
        
        if currency in min_limits and amount < min_limits[currency]:
            self.logger.warning(f"Сумма ниже минимального лимита для {currency}: {amount}")
            return False
        
        if currency in max_limits and amount > max_limits[currency]:
            self.logger.warning(f"Превышен лимит для {currency}: {amount}")
            return False
        
        return True
    
    def _validate_sender_address(self, from_address: str) -> bool:
        if not self._validate_tron_address(from_address):
            return False
        
        blacklisted_addresses = os.getenv('BLACKLISTED_ADDRESSES', '').split(',')
        if from_address.lower() in [addr.lower().strip() for addr in blacklisted_addresses if addr.strip()]:
            self.logger.warning(f"Платеж от заблокированного адреса: {self._mask_wallet_address(from_address)}")
            return False
        
        if from_address.lower() == self.wallet_address.lower():
            self.logger.warning("Попытка самоперевода")
            return False
        
        return True
    
    def _validate_transaction_timestamp(self, transaction: Dict) -> bool:
        tx_timestamp = transaction.get('timestamp', 0)
        current_time = int(datetime.now().timestamp() * 1000)
        
        max_age = 60 * 60 * 1000
        
        if current_time - tx_timestamp > max_age:
            age_minutes = (current_time - tx_timestamp) / 1000 / 60
            self.logger.warning(f"Транзакция слишком старая: {age_minutes:.1f} минут")
            return False
        
        future_tolerance = 5 * 60 * 1000
        
        if tx_timestamp > current_time + future_tolerance:
            self.logger.warning("Транзакция из будущего")
            return False
        
        return True
    
    @retry_on_failure(max_retries=2, delay=2.0, exceptions=(Exception,))
    def _validate_transaction_confirmations(self, transaction: Dict) -> bool:
        min_confirmations = {
            'USDT': 19,
            'TRX': 19
        }
        
        currency = transaction.get('currency', '')
        required_confirmations = min_confirmations.get(currency, 19)
        
        try:
            tx_details = self.tronscan.get_transaction_details(transaction['transaction_id'])
            if not tx_details:
                self.logger.warning(f"Не удалось получить детали транзакции {transaction['transaction_id']}")
                return False
            
            confirmations = tx_details.get('confirmations', 0)
            if confirmations < required_confirmations:
                self.logger.info(f"Недостаточно подтверждений: {confirmations}/{required_confirmations}")
                return False
            
            return True
        except Exception as e:
            self.logger.error(f"Ошибка при проверке подтверждений: {e}")
            return False
    
    @retry_on_failure(max_retries=2, delay=1.0, exceptions=(Exception,))
    def _validate_usdt_contract(self, transaction: Dict) -> bool:
        if transaction.get('currency') != 'USDT':
            return True
        
        OFFICIAL_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
        
        try:
            tx_details = self.tronscan.get_transaction_details(transaction['transaction_id'])
            if not tx_details:
                return False
            
            transfers = tx_details.get('trc20TransferInfo', [])
            for transfer in transfers:
                token_info = transfer.get('tokenInfo', {})
                contract_address = token_info.get('tokenId', '')
                
                if contract_address != OFFICIAL_USDT_CONTRACT:
                    self.logger.warning(f"Поддельный USDT контракт: {contract_address}")
                    return False
            
            return True
        except Exception as e:
            self.logger.error(f"Ошибка при проверке USDT контракта: {e}")
            return False
    
    def _generate_payment_hash(self, form_id: str, amount: float, currency: str) -> str:
        data = f"{form_id}:{amount}:{currency}:{datetime.now().isoformat()}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]
    
    def _check_form_creation_limits(self, client_ip: str = None, user_id: str = None):
        
        try:
            active_forms_count = len(self.db.get_active_payment_forms(datetime.now().timestamp()))
            max_total_forms = 1000
            
            if active_forms_count >= max_total_forms:
                raise Exception(f"Превышен общий лимит активных форм: {active_forms_count}/{max_total_forms}")
            
            if client_ip:
                self.logger.info(f"Создание формы с IP: {client_ip}")
            
            if user_id:
                self.logger.info(f"Создание формы для пользователя: {user_id}")
            
            current_time = time.time()
            
            if hasattr(self, '_last_form_creation_time'):
                time_since_last = current_time - self._last_form_creation_time
                if time_since_last < 1.0:
                    raise Exception(f"Слишком частое создание форм. Подождите {1.0 - time_since_last:.1f} секунд")
            
            self._last_form_creation_time = current_time
            
        except Exception as e:
            self.logger.error(f"Превышен лимит создания форм: {e}")
            raise
    
    def _get_recent_transaction_amounts(self, currency: str) -> List[float]:
        try:
            current_time = datetime.now().timestamp()
            active_forms = self.db.get_active_payment_forms(current_time)
            recent_txs = self.db.get_pending_transactions()
            
            amounts = []
            
            for form in active_forms:
                if form['currency'] == currency:
                    amounts.append(form['amount'])
            
            for tx in recent_txs:
                if tx['currency'] == currency:
                    amounts.append(tx['amount'])
                    if len(amounts) >= 20:
                        break
            
            return amounts
        except Exception as e:
            self.logger.error(f"Ошибка при получении сумм активных форм и транзакций: {e}")
            return []
    
    def _get_blockchain_transaction_amounts(self, currency: str, days_back: int = 7) -> List[float]:
        try:
            since_timestamp = int((datetime.now() - timedelta(days=days_back)).timestamp() * 1000)
            
            blockchain_txs = self.tronscan.get_account_transactions(
                self.wallet_address, 
                limit=100,
                start=0
            )
            
            amounts = []
            for tx in blockchain_txs:
                tx_timestamp = tx.get('timestamp', 0)
                if tx_timestamp < since_timestamp:
                    continue
                    
                parsed_tx = self.tronscan.parse_transaction(tx)
                if parsed_tx and parsed_tx['currency'] == currency:
                    if parsed_tx['to_address'].lower() == self.wallet_address.lower():
                        amounts.append(parsed_tx['amount'])
                        if len(amounts) >= 50:
                            break
            
            self.logger.debug(f"Получено {len(amounts)} сумм из блокчейна за {days_back} дней")
            return amounts
            
        except Exception as e:
            self.logger.error(f"Ошибка при получении сумм из блокчейна: {e}")
            return []
    
    def _generate_unique_amount(self, base_amount: float, currency: str, max_attempts: int = 100) -> float:
        recent_amounts = self._get_recent_transaction_amounts(currency)
        
        for attempt in range(max_attempts):
            random_addition = secrets.randbelow(999999) / 1000000.0
            if random_addition < 0.000001:
                random_addition = 0.000001
            
            final_amount = round(base_amount + random_addition, 6)
            
            is_unique = True
            for recent_amount in recent_amounts:
                if abs(final_amount - recent_amount) < 0.000001:
                    is_unique = False
                    break
            
            if is_unique:
                self.logger.debug(f"Сгенерирована уникальная сумма: {final_amount} (попытка {attempt + 1})")
                return final_amount
        
        timestamp_suffix = int(datetime.now().timestamp() * 1000) % 1000000 / 1000000.0
        final_amount = round(base_amount + timestamp_suffix, 6)
        self.logger.warning(f"Использована временная метка для генерации суммы: {final_amount}")
        return final_amount
    
    def _check_recent_transactions(self, amount: float, currency: str) -> bool:
        try:
            local_amounts = self._get_recent_transaction_amounts(currency)
            
            for recent_amount in local_amounts:
                if abs(amount - recent_amount) < 0.01:
                    self.logger.warning(f"Сумма {amount} слишком похожа на локальную транзакцию: {recent_amount}")
                    return False
            
            blockchain_amounts = self._get_blockchain_transaction_amounts(currency)
            
            for blockchain_amount in blockchain_amounts:
                if abs(amount - blockchain_amount) < 0.01:
                    self.logger.warning(f"Сумма {amount} слишком похожа на блокчейн транзакцию: {blockchain_amount}")
                    return False
            
            return True
        except Exception as e:
            self.logger.error(f"Ошибка при проверке последних транзакций: {e}")
            return True
    
    def create_payment_form(self, amount: float, currency: str = "TRX", 
                          description: str = "", expires_hours: int = 24, 
                          client_ip: str = None, user_id: str = None) -> Dict:
        
        if not isinstance(amount, (int, float)):
            raise ValueError(f"amount должен быть числом, получен {type(amount)}")
            
        if not isinstance(currency, str):
            raise ValueError(f"currency должен быть строкой, получен {type(currency)}")
            
        if not isinstance(description, str):
            raise ValueError(f"description должен быть строкой, получен {type(description)}")
            
        if not isinstance(expires_hours, int):
            raise ValueError(f"expires_hours должен быть целым числом, получен {type(expires_hours)}")
            
        if client_ip is not None and not isinstance(client_ip, str):
            raise ValueError(f"client_ip должен быть строкой или None, получен {type(client_ip)}")
            
        if user_id is not None and not isinstance(user_id, str):
            raise ValueError(f"user_id должен быть строкой или None, получен {type(user_id)}")
        
        self._check_form_creation_limits(client_ip, user_id)
        
        if not self._validate_amount(amount, currency):
            raise ValueError(f"Некорректная сумма: {amount} {currency}")
        
        if not self._validate_description(description):
            raise ValueError("Некорректное описание платежа")
        
        if not self._validate_tron_address(self.wallet_address):
            raise ValueError(f"Некорректный адрес кошелька: {self.wallet_address}")
        
        if currency not in ['TRX', 'USDT']:
            raise ValueError(f"Неподдерживаемая валюта: {currency}")
        
        if expires_hours < 1 or expires_hours > 168:
            raise ValueError(f"Некорректное время истечения: {expires_hours} часов")
        
        if not self._check_recent_transactions(amount, currency):
            raise Exception("Сумма слишком похожа на недавние транзакции. Попробуйте другую сумму.")
        
        final_amount = self._generate_unique_amount(amount, currency)
        
        form_id = str(uuid.uuid4())
        
        success = self.db.create_payment_form(
            form_id=form_id,
            amount=final_amount,
            currency=currency,
            description=description,
            wallet_address=self.wallet_address,
            expires_hours=expires_hours
        )
        
        if success:
            self.logger.info(f"Создана платежная форма {form_id}: {self._mask_amount(final_amount)} {currency}")
            return {
                'form_id': form_id,
                'amount': final_amount,
                'original_amount': amount,
                'currency': currency,
                'description': description,
                'wallet_address': self.wallet_address,
                'expires_at': datetime.now() + timedelta(hours=expires_hours),
                'status': 'pending'
            }
        else:
            raise Exception("Не удалось создать платежную форму")
    
    def get_payment_form(self, form_id: str) -> Optional[Dict]:
        return self.db.get_payment_form(form_id)
    
    def generate_payment_url(self, form_id: str) -> str:
        form_data = self.get_payment_form(form_id)
        if not form_data:
            raise ValueError("Платежная форма не найдена")
        
        amount = form_data['amount']
        currency = form_data['currency']
        
        if currency == "TRX":
            return f"tronlink://send?address={self.wallet_address}&amount={amount}"
        elif currency == "USDT":
            usdt_contract = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            return f"tronlink://send?address={self.wallet_address}&amount={amount}&token={usdt_contract}"
        else:
            return f"tronlink://send?address={self.wallet_address}&amount={amount}&token={currency}"
    
    def generate_payment_qr_data(self, form_id: str) -> str:
        form_data = self.get_payment_form(form_id)
        if not form_data:
            raise ValueError("Платежная форма не найдена")
        
        amount = form_data['amount']
        currency = form_data['currency']
        
        if currency == "TRX":
            return f"tron:{self.wallet_address}?amount={amount}"
        elif currency == "USDT":
            usdt_contract = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            return f"tron:{self.wallet_address}?amount={amount}&token={usdt_contract}"
        else:
            return f"tron:{self.wallet_address}?amount={amount}&token={currency}"
    
    def check_payment_status(self, form_id: str) -> Dict:
        form_data = self.get_payment_form(form_id)
        if not form_data:
            return {'status': 'not_found'}
        
        if datetime.now().timestamp() > float(form_data['expires_at']):
            return {'status': 'expired'}
        
        transactions = self.db.get_transactions_by_form(form_id)
        
        if transactions:
            latest_tx = transactions[0]
            if latest_tx['status'] == 'confirmed':
                return {
                    'status': 'paid',
                    'transaction_id': latest_tx['transaction_id'],
                    'amount': latest_tx['amount'],
                    'currency': latest_tx['currency']
                }
            elif latest_tx['status'] == 'pending':
                return {
                    'status': 'pending',
                    'transaction_id': latest_tx['transaction_id']
                }
        
        return {'status': 'waiting'}
    
    def start_monitoring(self, check_interval: int = 30):
        if self.monitoring:
            return
        
        self.monitoring = True
        self.monitor_thread = threading.Thread(
            target=self._monitor_payments,
            args=(check_interval,),
            daemon=True
        )
        self.monitor_thread.start()
        self.logger.info("Мониторинг платежей запущен")
    
    def stop_monitoring(self):
        self.monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join()
        self.logger.info("Мониторинг платежей остановлен")
    
    def _monitor_payments(self, check_interval: int):
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        while self.monitoring:
            try:
                pending_forms = self._get_active_payment_forms()
                
                if not pending_forms:
                    self.logger.debug("💤 Нет активных платежных форм для мониторинга")
                    time.sleep(check_interval)
                    continue
                
                self.logger.info(f"🔄 Начат цикл мониторинга: проверяем {len(pending_forms)} активных форм")
                
                for form_data in pending_forms:
                    if not self.monitoring:
                        break
                        
                    form_id = form_data['form_id']
                    
                    try:
                        since_hours = 1
                        since_timestamp = int((datetime.now() - timedelta(hours=since_hours)).timestamp() * 1000)
                        
                        self.logger.debug(f"🔍 Проверка транзакций для формы {form_id} за последние {since_hours} часов")
                        
                        
                        recent_txs = self.tronscan.check_recent_transactions(
                            self.wallet_address, 
                            since_timestamp=since_timestamp
                        )
                        
                        self.logger.info(f"📥 Получено {len(recent_txs)} транзакций за {since_hours}ч для проверки формы {form_id}")
                        
                        
                        for i, tx in enumerate(recent_txs):
                            if not self.monitoring:
                                break
                                
                            tx_hash = tx.get('hash', 'unknown')
                            self.logger.debug(f"🔄 Обработка транзакции {i+1}/{len(recent_txs)}: {tx_hash[:16]}...")
                                
                            try:
                                parsed_tx = self.tronscan.parse_transaction(tx)
                                
                                if not parsed_tx:
                                    self.logger.debug(f"⚠️  Транзакция {tx_hash[:16]} не удалось распарсить")
                                    continue
                                
                                self.logger.info(f"💰 Распарсена транзакция: {parsed_tx['amount']} {parsed_tx['currency']} "
                                                f"от {self._mask_wallet_address(parsed_tx['from_address'])} "
                                                f"к {self._mask_wallet_address(parsed_tx['to_address'])}")
                                
                                
                                if self._is_payment_for_form(parsed_tx, form_data):
                                    self.logger.info(f"✅ Найден подходящий платеж для формы {form_id}!")
                                    self._process_payment(parsed_tx, form_id)
                                else:
                                    self.logger.debug(f"❌ Транзакция не подходит для формы {form_id}")
                                    
                            except Exception as e:
                                self.logger.error(f"💥 Ошибка при обработке транзакции {tx_hash[:16]}: {e}")
                                continue
                                
                    except Exception as e:
                        self.logger.error(f"Ошибка при получении транзакций для формы {form_id}: {e}")
                        continue
                
                consecutive_errors = 0
                time.sleep(check_interval)
                
            except KeyboardInterrupt:
                self.logger.info("Мониторинг остановлен пользователем")
                break
            except Exception as e:
                consecutive_errors += 1
                error_sleep = min(check_interval * consecutive_errors, 300)
                
                self.logger.error(f"Критическая ошибка при мониторинге (#{consecutive_errors}): {e}")
                
                if consecutive_errors >= max_consecutive_errors:
                    self.logger.critical(f"Превышено максимальное количество последовательных ошибок ({max_consecutive_errors}). Остановка мониторинга.")
                    self.monitoring = False
                    break
                
                self.logger.info(f"Пауза {error_sleep} секунд перед повтором мониторинга")
                time.sleep(error_sleep)
    
    def _get_active_payment_forms(self) -> list:
        try:
            current_time = datetime.now().timestamp()
            return self.db.get_active_payment_forms(current_time)
        except Exception as e:
            self.logger.error(f"Ошибка при получении активных форм: {e}")
            return []
    
    def _is_payment_for_form(self, transaction: Dict, form_data: Dict) -> bool:
        tx_id = transaction['transaction_id']
        form_id = form_data['form_id']
        
        self.logger.debug(f"🔍 Проверка соответствия транзакции {tx_id[:16]} форме {form_id[:8]}")
        
        existing_tx = self.db.get_transaction_by_id(transaction['transaction_id'])
        if existing_tx:
            self.logger.debug(f"🔄 Транзакция {tx_id[:16]} уже обработана ранее")
            return False
        
        from_address = transaction.get('from_address', '')
        if not self._validate_sender_address(from_address):
            self.logger.debug(f"🚫 Невалидный отправитель: {self._mask_wallet_address(from_address)}")
            return False
        
        if not self._validate_transaction_timestamp(transaction):
            self.logger.debug(f"⏰ Транзакция {tx_id[:16]} не прошла проверку времени")
            return False
        
        if not self._validate_transaction_confirmations(transaction):
            self.logger.debug(f"📋 Транзакция {tx_id[:16]} недостаточно подтверждений")
            return False
        
        if not self._validate_usdt_contract(transaction):
            self.logger.debug(f"📄 Транзакция {tx_id[:16]} неверный USDT контракт")
            return False
        
        tx_amount = transaction['amount']
        form_amount = form_data['amount'] 
        amount_match = abs(tx_amount - form_amount) < 0.000001
        
        tx_currency = transaction['currency']
        form_currency = form_data['currency']
        currency_match = tx_currency == form_currency
        
        tx_to_address = transaction.get('to_address', '').lower()
        wallet_address = self.wallet_address.lower()
        address_match = tx_to_address == wallet_address
        
        is_confirmed = transaction.get('confirmed', False)
        
        self.logger.debug(f"📊 Проверка соответствия для {tx_id[:16]}:")
        self.logger.debug(f"   💰 Сумма: {tx_amount} vs {form_amount} = {'✅' if amount_match else '❌'}")
        self.logger.debug(f"   💱 Валюта: {tx_currency} vs {form_currency} = {'✅' if currency_match else '❌'}")
        self.logger.debug(f"   📍 Адрес: {self._mask_wallet_address(tx_to_address)} vs {self._mask_wallet_address(wallet_address)} = {'✅' if address_match else '❌'}")
        self.logger.debug(f"   ✔️  Подтверждена: {'✅' if is_confirmed else '❌'}")
        
        if amount_match and currency_match and address_match and is_confirmed:
            self.logger.info(f"🎉 НАЙДЕН ПОДХОДЯЩИЙ ПЛАТЕЖ! Транзакция {tx_id[:16]} → Форма {form_id[:8]}")
            return True
        
        reasons = []
        if not amount_match:
            reasons.append(f"сумма ({tx_amount} ≠ {form_amount})")
        if not currency_match:
            reasons.append(f"валюта ({tx_currency} ≠ {form_currency})")
        if not address_match:
            reasons.append(f"адрес ({self._mask_wallet_address(tx_to_address)} ≠ {self._mask_wallet_address(wallet_address)})")
        if not is_confirmed:
            reasons.append("не подтверждена")
        
        self.logger.debug(f"❌ Транзакция {tx_id[:16]} отклонена: {', '.join(reasons)}")
        
        return False
    
    def _process_payment(self, transaction: Dict, form_id: str):
        result = self.db.process_payment_atomic(
            transaction_id=transaction['transaction_id'],
            from_address=transaction['from_address'],
            to_address=transaction['to_address'],
            amount=transaction['amount'],
            currency=transaction['currency'],
            form_id=form_id
        )
        
        if result['status'] == 'success':
            self.logger.info(f"Успешно обработан платеж для формы {form_id}: {self._mask_amount(transaction['amount'])} {transaction['currency']}")
            
            if form_id in self.payment_callbacks:
                try:
                    self.payment_callbacks[form_id](transaction, form_id)
                except Exception as e:
                    self.logger.error(f"Ошибка в callback для формы {form_id}: {e}")
        else:
            self.logger.warning(f"Не удалось обработать платеж для формы {form_id}: {result['message']}")
    
    def register_payment_callback(self, form_id: str, callback: Callable):
        self.payment_callbacks[form_id] = callback
    
    def unregister_payment_callback(self, form_id: str):
        if form_id in self.payment_callbacks:
            del self.payment_callbacks[form_id]
    
    def get_transaction_history(self, form_id: str = None) -> list:
        if form_id:
            return self.db.get_transactions_by_form(form_id)
        else:
            return self.db.get_pending_transactions()
    
    def _validate_ip_address(self, ip: str) -> bool:
        try:
            ipaddress.ip_address(ip)
            return True
        except ValueError:
            return False
