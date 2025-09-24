"""
Тест проверки корректности Win Rate в A/B тестировании
"""
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv

load_dotenv()

def test_ab_winrate():
    """Проверка расчета Win Rate для trailing stop в A/B тесте"""
    
    db_config = {
        'host': os.getenv('DB_HOST'),
        'port': os.getenv('DB_PORT', '5432'),
        'database': os.getenv('DB_NAME'),
        'user': os.getenv('DB_USER')
        # password не указываем, используется .pgpass
    }
    
    print("="*60)
    print("ТЕСТ WIN RATE В A/B ТЕСТИРОВАНИИ")
    print("="*60)
    
    try:
        conn = psycopg2.connect(**db_config)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Проверяем статистику по trailing stop
        cur.execute("""
            SELECT 
                close_reason,
                COUNT(*) as count,
                SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END) as profitable,
                SUM(CASE WHEN realized_pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                SUM(realized_pnl_usd) as total_pnl
            FROM web.web_signals
            WHERE is_closed = true
                AND signal_timestamp >= NOW() - INTERVAL '30 days'
            GROUP BY close_reason
            ORDER BY count DESC
        """)
        
        results = cur.fetchall()
        
        print("\n📊 СТАТИСТИКА ПО ТИПАМ ЗАКРЫТИЯ:")
        
        total_wins = 0
        total_losses = 0
        total_pnl = 0
        
        for r in results:
            reason = r['close_reason'] or 'unknown'
            print(f"\n{reason}:")
            print(f"  Всего: {r['count']}")
            print(f"  Прибыльных: {r['profitable']}")
            print(f"  Убыточных: {r['losses']}")
            print(f"  P&L: ${r['total_pnl']:.2f}")
            
            if reason == 'take_profit':
                total_wins += r['count']
            elif reason == 'stop_loss':
                total_losses += r['count']
            elif reason == 'trailing_stop':
                total_wins += r['profitable']
                total_losses += r['losses']
            
            total_pnl += r['total_pnl']
        
        print("\n✅ ИТОГОВЫЙ РАСЧЕТ WIN RATE:")
        print(f"  Всего прибыльных: {total_wins}")
        print(f"  Всего убыточных: {total_losses}")
        print(f"  Общий P&L: ${total_pnl:.2f}")
        
        total_closed = total_wins + total_losses
        if total_closed > 0:
            win_rate = (total_wins / total_closed) * 100
            print(f"  WIN RATE: {win_rate:.1f}%")
        else:
            print(f"  WIN RATE: Нет закрытых позиций")
        
        # Проверяем специфично для trailing_stop
        cur.execute("""
            SELECT 
                COUNT(*) as total_trailing,
                COUNT(CASE WHEN realized_pnl_usd > 0 THEN 1 END) as trailing_profitable,
                COUNT(CASE WHEN realized_pnl_usd <= 0 THEN 1 END) as trailing_loss,
                AVG(realized_pnl_usd) as avg_pnl,
                SUM(realized_pnl_usd) as total_pnl
            FROM web.web_signals
            WHERE close_reason = 'trailing_stop'
                AND signal_timestamp >= NOW() - INTERVAL '30 days'
        """)
        
        trailing = cur.fetchone()
        
        if trailing and trailing['total_trailing'] > 0:
            print(f"\n🎯 ДЕТАЛИЗАЦИЯ TRAILING STOP:")
            print(f"  Всего trailing: {trailing['total_trailing']}")
            print(f"  Прибыльных: {trailing['trailing_profitable']}")
            print(f"  Убыточных: {trailing['trailing_loss']}")
            print(f"  Средний P&L: ${trailing['avg_pnl']:.2f}")
            print(f"  Общий P&L: ${trailing['total_pnl']:.2f}")
            
            # Проверка корректности
            if trailing['total_pnl'] > 0 and trailing['trailing_profitable'] == 0:
                print("\n⚠️ ВНИМАНИЕ: Положительный P&L но 0 прибыльных сделок!")
                print("   Возможна ошибка в подсчете trailing_profitable")
        
        cur.close()
        conn.close()
        
        print("\n" + "="*60)
        print("РЕКОМЕНДАЦИИ:")
        print("="*60)
        print("""
1. Если Win Rate = 0% при положительном P&L:
   - Проверьте правильность записи trailing_profitable в БД
   - Убедитесь что process_scoring_signals_batch возвращает корректные stats
   
2. Для отладки в app.py добавьте логирование:
   print(f"Stats B: {stats_b}")
   
3. Проверьте что в БД корректно записывается close_reason='trailing_stop'
""")
        
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")

if __name__ == "__main__":
    test_ab_winrate()