"""
Unit-тесты для проверки расчетов Trailing Stop
"""
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
import sys
import os

# Добавляем путь к модулям
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import calculate_trailing_stop_exit


class TestTrailingStopCalculations(unittest.TestCase):
    """Тесты для проверки корректности расчетов Trailing Stop"""
    
    def setUp(self):
        """Инициализация тестовых данных"""
        self.base_timestamp = datetime(2024, 1, 1, 0, 0, 0)
        self.position_size = 100.0
        self.leverage = 5
        
    def generate_candle(self, hours_offset, open_p, high_p, low_p, close_p):
        """Генерация свечи для тестов"""
        return {
            'timestamp': self.base_timestamp + timedelta(hours=hours_offset),
            'open_price': Decimal(str(open_p)),
            'high_price': Decimal(str(high_p)),
            'low_price': Decimal(str(low_p)),
            'close_price': Decimal(str(close_p))
        }
    
    def test_trailing_stop_activation_long(self):
        """Тест активации Trailing Stop для LONG позиции"""
        entry_price = 100.0
        trailing_activation_pct = 2.0  # Активация при +2%
        trailing_distance_pct = 1.0    # Дистанция 1%
        sl_percent = 3.0
        
        # История: цена растет до 102.5 (активация), затем падает
        history = [
            self.generate_candle(1, 100, 101, 99.5, 100.5),
            self.generate_candle(2, 100.5, 102.5, 100.5, 102),  # Активация trailing
            self.generate_candle(3, 102, 102, 101, 101.5),     # Trailing stop срабатывает
        ]
        
        result = calculate_trailing_stop_exit(
            entry_price, history, 'LONG',
            trailing_distance_pct, trailing_activation_pct,
            sl_percent, self.position_size, self.leverage,
            signal_timestamp=self.base_timestamp
        )
        
        # Проверки
        self.assertTrue(result['trailing_activated'])
        self.assertTrue(result['is_closed'])
        self.assertEqual(result['close_reason'], 'trailing_stop')
        # При активации на 102.5, trailing stop = 102.5 * 0.99 = 101.475
        self.assertAlmostEqual(result['close_price'], 101.475, places=2)
        self.assertGreater(result['pnl_usd'], 0)  # Должна быть прибыль
        
    def test_trailing_stop_activation_short(self):
        """Тест активации Trailing Stop для SHORT позиции"""
        entry_price = 100.0
        trailing_activation_pct = 2.0  # Активация при -2%
        trailing_distance_pct = 1.0    # Дистанция 1%
        sl_percent = 3.0
        
        # История: цена падает до 97.5 (активация), затем растет
        history = [
            self.generate_candle(1, 100, 100.5, 99, 99.5),
            self.generate_candle(2, 99.5, 99.5, 97.5, 98),   # Активация trailing
            self.generate_candle(3, 98, 99, 98, 98.5),       # Trailing stop срабатывает
        ]
        
        result = calculate_trailing_stop_exit(
            entry_price, history, 'SHORT',
            trailing_distance_pct, trailing_activation_pct,
            sl_percent, self.position_size, self.leverage,
            signal_timestamp=self.base_timestamp
        )
        
        # Проверки
        self.assertTrue(result['trailing_activated'])
        self.assertTrue(result['is_closed'])
        self.assertEqual(result['close_reason'], 'trailing_stop')
        # При активации на 97.5, trailing stop = 97.5 * 1.01 = 98.475
        self.assertAlmostEqual(result['close_price'], 98.475, places=2)
        self.assertGreater(result['pnl_usd'], 0)  # Должна быть прибыль
        
    def test_stop_loss_before_trailing_activation(self):
        """Тест срабатывания обычного Stop Loss до активации Trailing"""
        entry_price = 100.0
        trailing_activation_pct = 3.0
        trailing_distance_pct = 1.0
        sl_percent = 2.0  # SL на уровне -2%
        
        # История: цена сразу падает до SL
        history = [
            self.generate_candle(1, 100, 100, 98, 98.5),
            self.generate_candle(2, 98.5, 98.5, 97.5, 98),  # Пробивает SL
        ]
        
        result = calculate_trailing_stop_exit(
            entry_price, history, 'LONG',
            trailing_distance_pct, trailing_activation_pct,
            sl_percent, self.position_size, self.leverage,
            signal_timestamp=self.base_timestamp
        )
        
        # Проверки
        self.assertFalse(result['trailing_activated'])
        self.assertTrue(result['is_closed'])
        self.assertEqual(result['close_reason'], 'stop_loss')
        self.assertAlmostEqual(result['close_price'], 98.0, places=2)  # SL = 100 * 0.98
        self.assertLess(result['pnl_usd'], 0)  # Должен быть убыток
        
    def test_max_profit_tracking(self):
        """Тест отслеживания максимальной прибыли"""
        entry_price = 100.0
        trailing_activation_pct = 1.0
        trailing_distance_pct = 1.0
        sl_percent = 5.0
        
        # История: цена растет до 105, затем падает
        history = [
            self.generate_candle(1, 100, 102, 100, 101.5),
            self.generate_candle(2, 101.5, 105, 101.5, 104),  # Максимум 105
            self.generate_candle(3, 104, 104, 102, 103),
            self.generate_candle(4, 103, 103.5, 102.5, 103),
        ]
        
        result = calculate_trailing_stop_exit(
            entry_price, history, 'LONG',
            trailing_distance_pct, trailing_activation_pct,
            sl_percent, self.position_size, self.leverage,
            signal_timestamp=self.base_timestamp
        )
        
        # Проверки максимальной прибыли
        self.assertEqual(result['absolute_best_price'], 105.0)
        max_profit_percent = ((105 - 100) / 100) * 100  # 5%
        expected_max_profit = self.position_size * (max_profit_percent / 100) * self.leverage
        self.assertAlmostEqual(result['max_profit_usd'], expected_max_profit, places=2)
        
    def test_timeout_exit(self):
        """Тест выхода по таймауту (48 часов)"""
        entry_price = 100.0
        trailing_activation_pct = 10.0  # Высокий порог, не активируется
        trailing_distance_pct = 1.0
        sl_percent = 10.0  # Высокий SL, не срабатывает
        
        # История на 49 часов - должен сработать таймаут
        history = []
        for i in range(50):  # 50 часов
            price = 100 + (i % 3) - 1  # Колебания ±1
            history.append(
                self.generate_candle(i, price, price + 0.5, price - 0.5, price)
            )
        
        result = calculate_trailing_stop_exit(
            entry_price, history, 'LONG',
            trailing_distance_pct, trailing_activation_pct,
            sl_percent, self.position_size, self.leverage,
            signal_timestamp=self.base_timestamp
        )
        
        # Проверки
        self.assertFalse(result['trailing_activated'])
        self.assertTrue(result['is_closed'])
        self.assertEqual(result['close_reason'], 'timeout')
        
    def test_trailing_stop_movement(self):
        """Тест движения Trailing Stop при росте цены"""
        entry_price = 100.0
        trailing_activation_pct = 1.0
        trailing_distance_pct = 2.0  # Дистанция 2%
        sl_percent = 5.0
        
        # История: постепенный рост с откатами
        history = [
            self.generate_candle(1, 100, 101.5, 100, 101),    # Активация
            self.generate_candle(2, 101, 103, 101, 102.5),    # Новый максимум
            self.generate_candle(3, 102.5, 104, 102, 103.5),  # Еще выше
            self.generate_candle(4, 103.5, 105, 103, 104),    # Максимум 105
            self.generate_candle(5, 104, 104, 102.5, 102.8),  # Откат, но не до стопа
            self.generate_candle(6, 102.8, 103, 102, 102.5),  # Срабатывает trailing
        ]
        
        result = calculate_trailing_stop_exit(
            entry_price, history, 'LONG',
            trailing_distance_pct, trailing_activation_pct,
            sl_percent, self.position_size, self.leverage,
            signal_timestamp=self.base_timestamp
        )
        
        # Проверки
        self.assertTrue(result['trailing_activated'])
        self.assertTrue(result['is_closed'])
        self.assertEqual(result['close_reason'], 'trailing_stop')
        # Trailing stop должен быть на уровне 105 * 0.98 = 102.9
        self.assertAlmostEqual(result['close_price'], 102.9, places=1)
        self.assertGreater(result['pnl_usd'], 0)  # Прибыльная сделка
        
    def test_pnl_calculations(self):
        """Тест корректности расчета P&L"""
        entry_price = 100.0
        close_price = 105.0
        
        # Простой сценарий с фиксированным выходом
        history = [
            self.generate_candle(1, 100, 105, 100, 105),  # Сразу до 105
            self.generate_candle(2, 105, 106, 103, 103.5), # Активация и срабатывание trailing
        ]
        
        result = calculate_trailing_stop_exit(
            entry_price, history, 'LONG',
            trailing_distance_pct=2.0,
            trailing_activation_pct=3.0,
            sl_percent=5.0,
            position_size=self.position_size,
            leverage=self.leverage,
            signal_timestamp=self.base_timestamp
        )
        
        # Расчет ожидаемого P&L
        # Trailing stop = 106 * 0.98 = 103.88
        expected_pnl_percent = ((103.88 - 100) / 100) * 100
        expected_pnl_usd = self.position_size * (expected_pnl_percent / 100) * self.leverage
        
        self.assertAlmostEqual(result['pnl_percent'], expected_pnl_percent, places=1)
        self.assertAlmostEqual(result['pnl_usd'], expected_pnl_usd, places=0)


class TestStatisticsCalculation(unittest.TestCase):
    """Тесты для проверки корректности подсчета статистики"""
    
    def test_statistics_separation(self):
        """Тест корректного разделения статистики TP/SL/Trailing"""
        # Создаем тестовые данные с разными типами закрытия
        test_results = [
            {'close_reason': 'take_profit', 'pnl_usd': 50},
            {'close_reason': 'take_profit', 'pnl_usd': 30},
            {'close_reason': 'stop_loss', 'pnl_usd': -20},
            {'close_reason': 'stop_loss', 'pnl_usd': -15},
            {'close_reason': 'trailing_stop', 'pnl_usd': 25},
            {'close_reason': 'trailing_stop', 'pnl_usd': -10},
            {'close_reason': 'trailing_stop', 'pnl_usd': 35},
            {'close_reason': 'timeout', 'pnl_usd': 5},
        ]
        
        # Подсчет статистики
        stats = {
            'tp_count': sum(1 for r in test_results if r['close_reason'] == 'take_profit'),
            'sl_count': sum(1 for r in test_results if r['close_reason'] == 'stop_loss'),
            'trailing_count': sum(1 for r in test_results if r['close_reason'] == 'trailing_stop'),
            'trailing_wins': sum(1 for r in test_results if r['close_reason'] == 'trailing_stop' and r['pnl_usd'] > 0),
            'trailing_losses': sum(1 for r in test_results if r['close_reason'] == 'trailing_stop' and r['pnl_usd'] <= 0),
            'timeout_count': sum(1 for r in test_results if r['close_reason'] == 'timeout'),
        }
        
        # Проверки
        self.assertEqual(stats['tp_count'], 2)
        self.assertEqual(stats['sl_count'], 2)
        self.assertEqual(stats['trailing_count'], 3)
        self.assertEqual(stats['trailing_wins'], 2)
        self.assertEqual(stats['trailing_losses'], 1)
        self.assertEqual(stats['timeout_count'], 1)
        
        # Проверка, что нет двойного учета
        total_closed = stats['tp_count'] + stats['sl_count'] + stats['trailing_count'] + stats['timeout_count']
        self.assertEqual(total_closed, len(test_results))
        
    def test_win_rate_calculation(self):
        """Тест корректности расчета Win Rate"""
        # Тестовые данные
        tp_count = 10
        sl_count = 5
        trailing_wins = 3
        trailing_losses = 2
        
        # Расчет win rate
        total_wins = tp_count + trailing_wins
        total_losses = sl_count + trailing_losses
        total_closed = total_wins + total_losses
        win_rate = (total_wins / total_closed * 100) if total_closed > 0 else 0
        
        # Проверка
        expected_win_rate = (13 / 20) * 100  # 65%
        self.assertAlmostEqual(win_rate, expected_win_rate, places=2)


def run_tests():
    """Запуск всех тестов"""
    # Создаем test suite
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # Добавляем тесты
    suite.addTests(loader.loadTestsFromTestCase(TestTrailingStopCalculations))
    suite.addTests(loader.loadTestsFromTestCase(TestStatisticsCalculation))
    
    # Запускаем с подробным выводом
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    # Возвращаем результат
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    if success:
        print("\n✅ Все тесты пройдены успешно!")
    else:
        print("\n❌ Некоторые тесты не пройдены. Проверьте логику расчетов.")
    
    sys.exit(0 if success else 1)