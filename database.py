import sqlite3
import os
import threading
import logging
import queue
import contextlib
from datetime import datetime
from typing import List, Dict, Optional

class DatabaseManager:
    def __init__(self, db_path: str = "transaction.db", pool_size: int = 5):
        self.db_path = db_path
        self._lock = threading.RLock()
        self.logger = logging.getLogger(__name__)
        
        self.pool_size = pool_size
        self.connection_pool = queue.Queue(maxsize=pool_size)
        self.pool_lock = threading.Lock()
        
        self.init_database()
        self._init_connection_pool()
    
    def _init_connection_pool(self):
        for _ in range(self.pool_size):
            conn = self._create_connection()
            self.connection_pool.put(conn)
    
    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False
        )
        
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA cache_size=10000')
        conn.execute('PRAGMA temp_store=MEMORY')
        conn.execute('PRAGMA mmap_size=268435456')
        
        return conn
    
    @contextlib.contextmanager
    def get_connection(self):
        conn = None
        try:
            conn = self.connection_pool.get(timeout=10.0)
            
            try:
                conn.execute('SELECT 1')
            except sqlite3.Error:
                conn.close()
                conn = self._create_connection()
            
            yield conn
            
        except queue.Empty:
            self.logger.warning("Connection pool exhausted, creating temporary connection")
            conn = self._create_connection()
            yield conn
            
        finally:
            if conn:
                try:
                    self.connection_pool.put_nowait(conn)
                except queue.Full:
                    conn.close()
    
    def init_database(self):
        conn = self._create_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id TEXT UNIQUE NOT NULL,
                from_address TEXT NOT NULL,
                to_address TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                payment_form_id TEXT,
                description TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payment_forms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                form_id TEXT UNIQUE NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                wallet_address TEXT NOT NULL
            )
        ''')
        
        try:
            cursor.execute('ALTER TABLE payment_forms ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
            self.logger.info("Добавлена колонка updated_at в таблицу payment_forms")
        except sqlite3.OperationalError:
            pass
        
        conn.commit()
        conn.close()
    
    def create_payment_form(self, form_id: str, amount: float, currency: str, 
                          description: str, wallet_address: str, expires_hours: int = 24) -> bool:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                expires_at = datetime.now().timestamp() + (expires_hours * 3600)
                
                cursor.execute('''
                    INSERT INTO payment_forms 
                    (form_id, amount, currency, description, status, expires_at, wallet_address)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (form_id, amount, currency, description, 'pending', expires_at, wallet_address))
                
                conn.commit()
                return True
        except sqlite3.IntegrityError:
            return False
        except Exception as e:
            self.logger.error(f"Ошибка при создании платежной формы: {e}")
            return False
    
    def get_payment_form(self, form_id: str) -> Optional[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT * FROM payment_forms WHERE form_id = ?
            ''', (form_id,))
            
            row = cursor.fetchone()
            
            if row:
                columns = [description[0] for description in cursor.description]
                return dict(zip(columns, row))
            
            return None
    
    def process_payment_atomic(self, transaction_id: str, from_address: str, to_address: str,
                              amount: float, currency: str, form_id: str) -> Dict[str, str]:
        with self._lock:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                try:
                    cursor.execute('BEGIN IMMEDIATE')
                    
                    cursor.execute('SELECT id FROM transactions WHERE transaction_id = ?', (transaction_id,))
                    if cursor.fetchone():
                        conn.rollback()
                        return {'status': 'error', 'message': 'Transaction already processed'}
                    
                    cursor.execute('''
                        SELECT status, expires_at, amount, currency 
                        FROM payment_forms 
                        WHERE form_id = ?
                    ''', (form_id,))
                    
                    form_row = cursor.fetchone()
                    if not form_row:
                        conn.rollback()
                        return {'status': 'error', 'message': 'Payment form not found'}
                    
                    form_status, expires_at, expected_amount, expected_currency = form_row
                    
                    if form_status != 'pending':
                        conn.rollback()
                        return {'status': 'error', 'message': 'Payment form not pending'}
                    
                    if datetime.now().timestamp() > expires_at:
                        conn.rollback()
                        return {'status': 'error', 'message': 'Payment form expired'}
                    
                    if abs(amount - expected_amount) > 0.0001 or currency != expected_currency:
                        conn.rollback()
                        return {'status': 'error', 'message': 'Amount or currency mismatch'}
                    
                    cursor.execute('''
                        INSERT INTO transactions 
                        (transaction_id, from_address, to_address, amount, currency, status, payment_form_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (transaction_id, from_address, to_address, amount, currency, 'confirmed', form_id))
                    
                    cursor.execute('''
                        UPDATE payment_forms 
                        SET status = 'paid', updated_at = CURRENT_TIMESTAMP 
                        WHERE form_id = ?
                    ''', (form_id,))
                    
                    conn.commit()
                    return {'status': 'success', 'message': 'Payment processed successfully'}
                    
                except sqlite3.Error as e:
                    conn.rollback()
                    return {'status': 'error', 'message': f'Database error: {e}'}
    
    def add_transaction(self, transaction_id: str, from_address: str, to_address: str,
                       amount: float, currency: str, status: str, 
                       payment_form_id: str = None, description: str = None) -> bool:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                    INSERT INTO transactions 
                    (transaction_id, from_address, to_address, amount, currency, status, payment_form_id, description)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (transaction_id, from_address, to_address, amount, currency, status, payment_form_id, description))
                
                conn.commit()
                return True
        except sqlite3.IntegrityError:
            return False
        except Exception as e:
            self.logger.error(f"Ошибка при добавлении транзакции: {e}")
            return False
    
    def get_transactions_by_form(self, form_id: str) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT * FROM transactions WHERE payment_form_id = ? ORDER BY created_at DESC
            ''', (form_id,))
            
            rows = cursor.fetchall()
            columns = [description[0] for description in cursor.description]
            
            return [dict(zip(columns, row)) for row in rows]
    
    def get_transaction_by_id(self, transaction_id: str) -> Optional[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT * FROM transactions WHERE transaction_id = ?
            ''', (transaction_id,))
            
            row = cursor.fetchone()
            
            if row:
                columns = [description[0] for description in cursor.description]
                return dict(zip(columns, row))
            
            return None
    
    def update_transaction_status(self, transaction_id: str, status: str) -> bool:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                    UPDATE transactions 
                    SET status = ?, updated_at = CURRENT_TIMESTAMP 
                    WHERE transaction_id = ?
                ''', (status, transaction_id))
                
                conn.commit()
                return True
        except Exception as e:
            self.logger.error(f"Ошибка при обновлении статуса транзакции: {e}")
            return False
    
    def get_pending_transactions(self) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT * FROM transactions WHERE status = 'pending' ORDER BY created_at DESC
            ''')
            
            rows = cursor.fetchall()
            columns = [description[0] for description in cursor.description]
            
            return [dict(zip(columns, row)) for row in rows]
    
    def get_active_payment_forms(self, current_time: float) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT * FROM payment_forms 
                WHERE status = 'pending' AND expires_at > ?
                ORDER BY created_at DESC
            ''', (current_time,))
            
            rows = cursor.fetchall()
            columns = [description[0] for description in cursor.description]
            
            return [dict(zip(columns, row)) for row in rows]
    
    def close_pool(self):
        while not self.connection_pool.empty():
            try:
                conn = self.connection_pool.get_nowait()
                conn.close()
            except queue.Empty:
                break
        self.logger.info("Connection pool closed")