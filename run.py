#!/usr/bin/env python3
"""
Скрипт запуска приложения "Помощник Трейдера"
"""
import os
import sys
from dotenv import load_dotenv
from config import Config

# Загрузка переменных окружения
load_dotenv()

# Проверка наличия обязательных параметров через Config
if not (Config.DB_HOST and Config.DB_NAME and Config.DB_USER):
    # Проверяем fallback на DATABASE_URL
    if not os.getenv('DATABASE_URL'):
        print("❌ Ошибка: Не установлены параметры подключения к базе данных.")
        print("\nВы должны установить в .env файле:")
        print("   - DB_HOST")
        print("   - DB_PORT (опционально, по умолчанию 5432)")
        print("   - DB_NAME")
        print("   - DB_USER")
        print("   - DB_PASSWORD (опционально, если используется .pgpass)")
        print("\nИли использовать DATABASE_URL для единой строки подключения.")
        print("\nСкопируйте .env.example в .env и настройте параметры.")
        sys.exit(1)

if Config.SECRET_KEY == 'dev-secret-key-change-in-production':
    print("⚠️  Внимание: SECRET_KEY не установлен, используется значение по умолчанию!")
    print("   Для production обязательно установите SECRET_KEY в .env файле.")

# Импорт и запуск приложения
try:
    from app import app
    
    print("🚀 Запуск приложения 'Помощник Трейдера'")
    print(f"📍 Адрес: http://localhost:{Config.PORT}")
    print(f"🔧 Режим отладки: {'включен' if Config.FLASK_DEBUG else 'выключен'}")
    print(f"💾 База данных: {Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_NAME}")
    print(f"🔌 Пул соединений: min={Config.DB_POOL_MIN_SIZE}, max={Config.DB_POOL_MAX_SIZE}")
    print("=" * 50)
    
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.FLASK_DEBUG)
    
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    print("Убедитесь, что все зависимости установлены:")
    print("pip install -r requirements.txt")
    sys.exit(1)
except Exception as e:
    print(f"❌ Ошибка запуска: {e}")
    sys.exit(1)
