#!/usr/bin/env python3
"""
Скрипт для проверки подключения к базе данных
"""
import os
import sys
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

def test_connection():
    """Тестирование подключения к базе данных"""
    
    # Проверка конфигурации
    print("=== Проверка конфигурации ===")
    
    db_host = os.getenv('DB_HOST')
    db_port = os.getenv('DB_PORT', '5432')
    db_name = os.getenv('DB_NAME')
    db_user = os.getenv('DB_USER')
    db_password = os.getenv('DB_PASSWORD')
    
    print(f"DB_HOST: {db_host}")
    print(f"DB_PORT: {db_port}")
    print(f"DB_NAME: {db_name}")
    print(f"DB_USER: {db_user}")
    print(f"DB_PASSWORD: {'***' if db_password else 'не установлен (используется .pgpass)'}")
    
    # Проверка .pgpass
    pgpass_file = os.path.expanduser('~/.pgpass')
    if os.path.exists(pgpass_file):
        print(f"\n.pgpass файл найден: {pgpass_file}")
        # Проверка прав доступа
        import stat
        mode = oct(os.stat(pgpass_file).st_mode)[-3:]
        if mode == '600':
            print("✓ Права доступа корректные (600)")
        else:
            print(f"✗ Неправильные права доступа: {mode} (должно быть 600)")
    else:
        print(f"\n.pgpass файл не найден: {pgpass_file}")
    
    # Тест подключения
    print("\n=== Тест подключения ===")
    
    try:
        from database import Database
        
        # Пробуем подключиться без пула для простого теста
        db = Database(
            host=db_host,
            port=db_port,
            database=db_name,
            user=db_user,
            password=db_password,
            use_pool=False
        )
        
        # Выполняем простой запрос
        result = db.execute_query("SELECT version()", fetch=True)
        print(f"✓ Подключение успешно!")
        print(f"Результат запроса: {result}")
        if result and len(result) > 0:
            if isinstance(result[0], dict):
                print(f"PostgreSQL версия: {result[0].get('version', 'неизвестно')}")
            elif isinstance(result[0], (list, tuple)) and len(result[0]) > 0:
                print(f"PostgreSQL версия: {result[0][0]}")
            else:
                print("PostgreSQL версия: неизвестно")
        else:
            print("PostgreSQL версия: неизвестно")
        
        # Проверяем SSL статус через pg_stat_ssl
        try:
            ssl_result = db.execute_query("""
                SELECT ssl, version, cipher, bits
                FROM pg_stat_ssl
                WHERE pid = pg_backend_pid()
            """, fetch=True)
            if ssl_result and len(ssl_result) > 0:
                ssl_info = ssl_result[0]
                print(f"SSL используется: {'Да' if ssl_info.get('ssl') else 'Нет'}")
                if ssl_info.get('ssl'):
                    print(f"  Версия: {ssl_info.get('version', 'н/д')}")
                    print(f"  Шифр: {ssl_info.get('cipher', 'н/д')}")
                    print(f"  Биты: {ssl_info.get('bits', 'н/д')}")
            else:
                print("SSL статус: не удалось определить")
        except Exception as e:
            print(f"SSL статус: не удалось определить ({e})")
        
        # Проверяем текущие настройки SSL
        try:
            settings = db.execute_query("""
                SELECT name, setting 
                FROM pg_settings 
                WHERE name IN ('ssl', 'ssl_ciphers', 'ssl_prefer_server_ciphers')
            """, fetch=True)
            
            if settings:
                print("\nНастройки SSL сервера:")
                for row in settings:
                    if isinstance(row, dict):
                        print(f"  {row.get('name', 'н/д')}: {row.get('setting', 'н/д')}")
        except Exception as e:
            print(f"Не удалось получить настройки SSL: {e}")
        
        return True
        
    except Exception as e:
        print(f"✗ Ошибка подключения: {e}")
        print(f"Тип ошибки: {type(e).__name__}")
        
        # Дополнительная диагностика
        if "decryption failed" in str(e):
            print("\nПроблема с SSL/TLS шифрованием. Возможные причины:")
            print("1. Несоответствие версий SSL между клиентом и сервером")
            print("2. Проблемы с сертификатами")
            print("3. Неправильные настройки sslmode")
            print("\nПопробуйте изменить sslmode в database.py на одно из:")
            print("  - disable (без SSL)")
            print("  - allow (попытка с SSL, откат на обычное)")
            print("  - prefer (предпочтительно SSL)")
            print("  - require (требуется SSL)")
        
        return False

if __name__ == "__main__":
    success = test_connection()
    sys.exit(0 if success else 1)