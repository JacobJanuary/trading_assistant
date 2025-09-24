"""
Применяет таблицу для кэширования прогресса анализа
"""
import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

def apply_analysis_cache_table():
    """Создает таблицу для хранения промежуточных результатов анализа"""
    
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
        
        print("Создание таблицы для кэширования прогресса анализа...")
        
        # Создаем таблицу
        cur.execute("""
            CREATE TABLE IF NOT EXISTS web.analysis_progress (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                analysis_type VARCHAR(50) NOT NULL,
                parameters JSONB NOT NULL,
                last_processed_combination INTEGER DEFAULT 0,
                total_combinations INTEGER NOT NULL,
                results JSONB,
                completed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, analysis_type)
            )
        """)
        print("✅ Таблица web.analysis_progress создана")
        
        # Создаем индекс
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_analysis_progress_user_type 
            ON web.analysis_progress(user_id, analysis_type)
        """)
        print("✅ Индекс idx_analysis_progress_user_type создан")
        
        # Создаем функцию для обновления updated_at
        cur.execute("""
            CREATE OR REPLACE FUNCTION update_analysis_progress_updated_at()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """)
        print("✅ Функция update_analysis_progress_updated_at создана")
        
        # Создаем триггер
        cur.execute("""
            DROP TRIGGER IF EXISTS update_analysis_progress_updated_at ON web.analysis_progress
        """)
        cur.execute("""
            CREATE TRIGGER update_analysis_progress_updated_at
            BEFORE UPDATE ON web.analysis_progress
            FOR EACH ROW
            EXECUTE FUNCTION update_analysis_progress_updated_at()
        """)
        print("✅ Триггер update_analysis_progress_updated_at создан")
        
        # Очищаем старые незавершенные записи
        cur.execute("""
            DELETE FROM web.analysis_progress 
            WHERE completed = FALSE 
            AND updated_at < NOW() - INTERVAL '24 hours'
        """)
        deleted = cur.rowcount
        if deleted > 0:
            print(f"✅ Удалено {deleted} старых незавершенных записей")
        
        conn.commit()
        
        # Проверяем таблицу
        cur.execute("""
            SELECT COUNT(*) as total,
                   COUNT(CASE WHEN completed THEN 1 END) as completed,
                   COUNT(CASE WHEN NOT completed THEN 1 END) as in_progress
            FROM web.analysis_progress
        """)
        stats = cur.fetchone()
        
        print("\n📊 Статистика analysis_progress:")
        print(f"  Всего записей: {stats[0]}")
        print(f"  Завершенных: {stats[1]}")
        print(f"  В процессе: {stats[2]}")
        
        cur.close()
        conn.close()
        
        print("\n✅ Таблица кэширования прогресса успешно создана!")
        print("Теперь анализ будет сохранять промежуточные результаты и продолжать")
        print("работу после разрыва соединения.")
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    apply_analysis_cache_table()