"""
Скрипт для оптимизации анализа эффективности
Создает индексы и оптимизирует структуру БД для ускорения анализа
"""
import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

def optimize_database():
    """Оптимизирует базу данных для ускорения анализа эффективности"""
    
    db_config = {
        'host': os.getenv('DB_HOST'),
        'port': os.getenv('DB_PORT', '5432'),
        'database': os.getenv('DB_NAME'),
        'user': os.getenv('DB_USER')
        # password не указываем, используется .pgpass
    }
    
    try:
        conn = psycopg2.connect(**db_config)
        cur = conn.cursor()
        
        print("Оптимизация базы данных для анализа эффективности...")
        
        # Создаем индексы если их нет
        indexes = [
            # Индекс для scoring_analysis_results
            ("idx_scoring_session_user", 
             "CREATE INDEX IF NOT EXISTS idx_scoring_session_user ON web.scoring_analysis_results(session_id, user_id)"),
            
            ("idx_scoring_closed", 
             "CREATE INDEX IF NOT EXISTS idx_scoring_closed ON web.scoring_analysis_results(is_closed, close_reason)"),
            
            # Индекс для web_signals
            ("idx_signals_timestamp", 
             "CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON web.web_signals(signal_timestamp)"),
            
            ("idx_signals_scores", 
             "CREATE INDEX IF NOT EXISTS idx_signals_scores ON web.web_signals(score_week, score_month)"),
            
            # Индекс для efficiency_cache
            ("idx_efficiency_cache", 
             "CREATE INDEX IF NOT EXISTS idx_efficiency_cache ON web.efficiency_cache(cache_key, user_id, created_at)")
        ]
        
        for idx_name, idx_sql in indexes:
            print(f"  Создание индекса {idx_name}...")
            try:
                cur.execute(idx_sql)
                conn.commit()
                print(f"    ✅ {idx_name} создан/проверен")
            except Exception as e:
                print(f"    ⚠️ {idx_name}: {e}")
                conn.rollback()
        
        # Обновляем статистику таблиц
        tables = [
            'web.scoring_analysis_results',
            'web.web_signals',
            'web.efficiency_cache'
        ]
        
        print("\nОбновление статистики таблиц...")
        for table in tables:
            try:
                cur.execute(f"ANALYZE {table}")
                conn.commit()
                print(f"  ✅ {table} проанализирована")
            except Exception as e:
                print(f"  ⚠️ {table}: {e}")
                conn.rollback()
        
        # Проверяем размеры таблиц
        print("\nРазмеры таблиц:")
        for table in tables:
            cur.execute(f"""
                SELECT 
                    pg_size_pretty(pg_total_relation_size('{table}')) as size,
                    COUNT(*) as rows
                FROM {table}
            """)
            result = cur.fetchone()
            print(f"  {table}: {result[0]}, строк: {result[1]}")
        
        # Рекомендации
        print("\n" + "="*60)
        print("РЕКОМЕНДАЦИИ ДЛЯ УСКОРЕНИЯ АНАЛИЗА:")
        print("="*60)
        print("""
1. Очистите старый кэш:
   DELETE FROM web.efficiency_cache WHERE created_at < NOW() - INTERVAL '7 days';

2. Очистите старые результаты анализа:
   DELETE FROM web.scoring_analysis_results 
   WHERE session_id LIKE 'eff_%' AND created_at < NOW() - INTERVAL '7 days';

3. Используйте анализ с меньшим шагом:
   Вместо шага 10% используйте 20% для первичного анализа

4. Анализируйте меньший диапазон:
   Вместо 60-90% попробуйте 70-80% для ключевых параметров
""")
        
        cur.close()
        conn.close()
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")

if __name__ == "__main__":
    optimize_database()