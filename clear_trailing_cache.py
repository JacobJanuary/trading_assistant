#!/usr/bin/env python3
"""
Скрипт для очистки кэша анализа Trailing Stop
"""

import psycopg2
from psycopg2 import sql
import os
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Параметры подключения к БД
DB_CONFIG = {
    'host': os.getenv('DB_HOST', '10.8.0.1'),
    'port': os.getenv('DB_PORT', 5432),
    'database': os.getenv('DB_NAME', 'fox_crypto'),
    'user': os.getenv('DB_USER', 'elcrypto'),
    'password': os.getenv('DB_PASSWORD', 'LohNeMamont@!21')
}

def clear_trailing_cache():
    """Очищает кэш анализа Trailing Stop"""
    conn = None
    cursor = None
    
    try:
        # Подключаемся к БД
        print("Подключение к базе данных...")
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()
        
        # Удаляем записи кэша для trailing stop
        print("Очистка кэша trailing stop анализа...")
        delete_query = """
            DELETE FROM web.efficiency_cache 
            WHERE cache_key LIKE 'trail_%'
        """
        cursor.execute(delete_query)
        deleted_rows = cursor.rowcount
        
        # Подтверждаем изменения
        conn.commit()
        
        print(f"✅ Успешно удалено {deleted_rows} записей из кэша")
        
        # Опционально: показываем оставшиеся записи
        cursor.execute("""
            SELECT COUNT(*) 
            FROM web.efficiency_cache 
            WHERE cache_key NOT LIKE 'trail_%'
        """)
        remaining = cursor.fetchone()[0]
        print(f"ℹ️  Осталось {remaining} записей других типов в кэше")
        
    except psycopg2.Error as e:
        print(f"❌ Ошибка при работе с БД: {e}")
        if conn:
            conn.rollback()
    except Exception as e:
        print(f"❌ Неожиданная ошибка: {e}")
    finally:
        # Закрываем соединения
        if cursor:
            cursor.close()
        if conn:
            conn.close()
        print("Соединение с БД закрыто")

def clear_all_cache():
    """Полная очистка всего кэша (опционально)"""
    response = input("Вы уверены, что хотите очистить ВЕСЬ кэш? (yes/no): ")
    if response.lower() != 'yes':
        print("Отменено")
        return
        
    conn = None
    cursor = None
    
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()
        
        print("Полная очистка кэша...")
        cursor.execute("DELETE FROM web.efficiency_cache")
        deleted_rows = cursor.rowcount
        
        conn.commit()
        print(f"✅ Весь кэш очищен. Удалено {deleted_rows} записей")
        
    except psycopg2.Error as e:
        print(f"❌ Ошибка при очистке кэша: {e}")
        if conn:
            conn.rollback()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

if __name__ == "__main__":
    print("=" * 50)
    print("ОЧИСТКА КЭША TRAILING STOP АНАЛИЗА")
    print("=" * 50)
    print()
    print("Выберите действие:")
    print("1. Очистить только кэш Trailing Stop анализа (рекомендуется)")
    print("2. Очистить ВЕСЬ кэш")
    print("3. Выход")
    print()
    
    choice = input("Введите номер (1-3): ")
    
    if choice == '1':
        clear_trailing_cache()
    elif choice == '2':
        clear_all_cache()
    elif choice == '3':
        print("Выход")
    else:
        print("Неверный выбор")