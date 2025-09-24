#!/usr/bin/env python3
"""
Скрипт запуска приложения "Помощник Трейдера"
"""
import os
import sys
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Проверка наличия обязательных переменных окружения
# Проверяем либо DATABASE_URL, либо набор отдельных параметров DB_*
has_database_url = bool(os.getenv('DATABASE_URL'))
has_separate_db_params = all([
    os.getenv('DB_HOST'),
    os.getenv('DB_NAME'), 
    os.getenv('DB_USER')
    # DB_PASSWORD не обязателен, если используется .pgpass
])

if not has_database_url and not has_separate_db_params:
    print("❌ Ошибка: Не установлены параметры подключения к базе данных.")
    print("\nВы должны установить один из вариантов:")
    print("\n1. Отдельные параметры (рекомендуется):")
    print("   - DB_HOST")
    print("   - DB_PORT (опционально, по умолчанию 5432)")
    print("   - DB_NAME")
    print("   - DB_USER")
    print("   - DB_PASSWORD (опционально, если используется .pgpass)")
    print("\n2. Единая строка подключения:")
    print("   - DATABASE_URL")
    print("\nСкопируйте .env.example в .env и настройте параметры.")
    sys.exit(1)

if not os.getenv('SECRET_KEY'):
    print("❌ Ошибка: Не установлена переменная окружения SECRET_KEY")
    print("\nСкопируйте .env.example в .env и настройте параметры.")
    sys.exit(1)

# Импорт и запуск приложения
try:
    from app import app
    
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    
    print("🚀 Запуск приложения 'Помощник Трейдера'")
    print(f"📍 Адрес: http://localhost:{port}")
    print(f"🔧 Режим отладки: {'включен' if debug else 'выключен'}")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=debug)
    
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    print("Убедитесь, что все зависимости установлены:")
    print("pip install -r requirements.txt")
    sys.exit(1)
except Exception as e:
    print(f"❌ Ошибка запуска: {e}")
    sys.exit(1)
