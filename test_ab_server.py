#!/usr/bin/env python3
"""
Скрипт для диагностики проблем A/B теста на сервере
"""
import sys
import os

print("="*60)
print("ДИАГНОСТИКА A/B ТЕСТА")
print("="*60)

# 1. Проверка импортов
print("\n1. Проверка импортов:")
try:
    import numpy as np
    print("  ✅ numpy установлен, версия:", np.__version__)
except ImportError as e:
    print("  ❌ numpy НЕ установлен:", e)
    print("     Установите: pip install numpy")

try:
    import scipy
    import scipy.stats
    print("  ✅ scipy установлен, версия:", scipy.__version__)
except ImportError as e:
    print("  ❌ scipy НЕ установлен:", e)
    print("     Установите: pip install scipy")

# 2. Проверка базы данных
print("\n2. Проверка базы данных:")
try:
    from dotenv import load_dotenv
    load_dotenv()
    
    import psycopg2
    
    db_config = {
        'host': os.getenv('DB_HOST'),
        'port': os.getenv('DB_PORT', '5432'),
        'database': os.getenv('DB_NAME'),
        'user': os.getenv('DB_USER')
        # password не указываем, используется .pgpass
    }
    
    conn = psycopg2.connect(**db_config)
    cur = conn.cursor()
    
    # Проверяем наличие сигналов
    cur.execute("""
        SELECT COUNT(*) 
        FROM fas.scoring_history 
        WHERE timestamp >= NOW() - INTERVAL '30 days'
            AND score_week > 65
            AND score_month > 64
    """)
    count = cur.fetchone()[0]
    print(f"  ✅ Подключение к БД успешно")
    print(f"     Найдено сигналов за 30 дней: {count}")
    
    cur.close()
    conn.close()
    
except Exception as e:
    print(f"  ❌ Ошибка БД: {e}")

# 3. Проверка Flask
print("\n3. Проверка Flask:")
try:
    import flask
    print("  ✅ Flask установлен, версия:", flask.__version__)
except ImportError as e:
    print("  ❌ Flask НЕ установлен:", e)

# 4. Проверка конфигурации nginx (если применимо)
print("\n4. Проверка веб-сервера:")
import subprocess
try:
    # Проверяем nginx
    result = subprocess.run(['nginx', '-v'], capture_output=True, text=True)
    if result.returncode == 0:
        print("  ✅ nginx установлен")
        print("     ВАЖНО: Убедитесь что в конфиге nginx есть:")
        print("     proxy_buffering off;")
        print("     proxy_cache off;")
        print("     proxy_set_header Connection '';")
        print("     proxy_http_version 1.1;")
        print("     chunked_transfer_encoding on;")
except:
    print("  ℹ️ nginx не найден или не используется")

# 5. Проверка Python версии
print("\n5. Версия Python:")
print(f"  Python {sys.version}")
if sys.version_info < (3, 7):
    print("  ⚠️ Рекомендуется Python 3.7+")

print("\n" + "="*60)
print("РЕКОМЕНДАЦИИ:")
print("="*60)

print("""
1. Если numpy/scipy не установлены:
   pip install numpy scipy

2. Для продакшн сервера с nginx добавьте в location блок:
   location /api/ab_test/ {
       proxy_pass http://localhost:5000;
       proxy_buffering off;
       proxy_cache off;
       proxy_set_header Connection '';
       proxy_http_version 1.1;
       chunked_transfer_encoding on;
       proxy_read_timeout 300s;
       proxy_connect_timeout 75s;
   }

3. Перезапустите сервисы:
   sudo systemctl restart nginx
   sudo systemctl restart your-flask-app

4. Проверьте логи:
   sudo tail -f /var/log/nginx/error.log
   sudo journalctl -u your-flask-app -f
""")

print("="*60)