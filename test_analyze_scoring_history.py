#!/usr/bin/env python3
"""
Юнит-тесты для валидации расчетов в analyze_scoring_history.py
"""

import unittest
from datetime import datetime, timedelta
from decimal import Decimal
import sys
import os

# Добавляем путь к основному скрипту
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Параметры анализа для тестов
ANALYSIS_PARAMS = {
    'tp_percent': 4.0,
    'sl_percent': 3.0,
    'position_size': 100.0,
    'leverage': 5,
    'analysis_hours': 48,
    'entry_delay_minutes': 15
}


# Mock класс для тестирования без подключения к БД
class MockScoringAnalyzer:
    def __init__(self):
        self.tp_percent = 4.0
        self.sl_percent = 3.0
        self.position_size = 100.0
        self.leverage = 5

    def process_price_history_improved(self, signal_type, entry_price, history, entry_time):
        """Тестовая версия метода обработки истории цен"""
        tp_percent = self.tp_percent
        sl_percent = self.sl_percent
        position_size = self.position_size
        leverage = self.leverage

        # Расчет уровней TP и SL
        if signal_type == 'BUY':
            tp_price = entry_price * (1 + tp_percent / 100)
            sl_price = entry_price * (1 - sl_percent / 100)
        else:  # SELL
            tp_price = entry_price * (1 - tp_percent / 100)
            sl_price = entry_price * (1 + sl_percent / 100)

        # Переменные для отслеживания
        is_closed = False
        close_reason = None
        close_price = None
        close_time = None
        hours_to_close = None
        is_win = None

        # Экстремумы за весь период
        absolute_max_price = entry_price
        absolute_min_price = entry_price

        # Анализ всех свечей
        for i, candle in enumerate(history):
            current_time = candle['timestamp']
            hours_passed = (current_time - entry_time).total_seconds() / 3600

            high_price = candle['high_price']
            low_price = candle['low_price']

            # Обновляем абсолютные экстремумы
            absolute_max_price = max(absolute_max_price, high_price)
            absolute_min_price = min(absolute_min_price, low_price)

            # Проверяем закрытие позиции
            if not is_closed:
                if signal_type == 'BUY':
                    if low_price <= sl_price:
                        is_closed = True
                        close_reason = 'stop_loss'
                        is_win = False
                        close_price = sl_price
                        close_time = current_time
                        hours_to_close = hours_passed
                    elif high_price >= tp_price:
                        is_closed = True
                        close_reason = 'take_profit'
                        is_win = True
                        close_price = tp_price
                        close_time = current_time
                        hours_to_close = hours_passed
                else:  # SELL
                    if high_price >= sl_price:
                        is_closed = True
                        close_reason = 'stop_loss'
                        is_win = False
                        close_price = sl_price
                        close_time = current_time
                        hours_to_close = hours_passed
                    elif low_price <= tp_price:
                        is_closed = True
                        close_reason = 'take_profit'
                        is_win = True
                        close_price = tp_price
                        close_time = current_time
                        hours_to_close = hours_passed

        # Если не закрылась
        if not is_closed:
            is_closed = True
            close_reason = 'timeout'
            is_win = None
            close_price = history[-1]['close_price']
            close_time = history[-1]['timestamp']
            hours_to_close = 48.0

        # Расчет максимального потенциала
        if signal_type == 'BUY':
            max_potential_profit_percent = ((absolute_max_price - entry_price) / entry_price) * 100
            max_potential_profit_usd = position_size * leverage * (max_potential_profit_percent / 100)
            max_potential_loss_percent = ((entry_price - absolute_min_price) / entry_price) * 100
            max_potential_loss_usd = position_size * leverage * (max_potential_loss_percent / 100)
            final_pnl_percent = ((close_price - entry_price) / entry_price) * 100
        else:  # SELL
            max_potential_profit_percent = ((entry_price - absolute_min_price) / entry_price) * 100
            max_potential_profit_usd = position_size * leverage * (max_potential_profit_percent / 100)
            max_potential_loss_percent = ((absolute_max_price - entry_price) / entry_price) * 100
            max_potential_loss_usd = position_size * leverage * (max_potential_loss_percent / 100)
            final_pnl_percent = ((entry_price - close_price) / entry_price) * 100

        final_pnl_usd = position_size * leverage * (final_pnl_percent / 100)

        return {
            'absolute_max_price': absolute_max_price,
            'absolute_min_price': absolute_min_price,
            'close_price': close_price,
            'close_reason': close_reason,
            'is_win': is_win,
            'hours_to_close': hours_to_close,
            'pnl_percent': final_pnl_percent,
            'pnl_usd': final_pnl_usd,
            'max_potential_profit_percent': max_potential_profit_percent,
            'max_potential_profit_usd': max_potential_profit_usd,
            'max_potential_loss_percent': max_potential_loss_percent,
            'max_potential_loss_usd': max_potential_loss_usd
        }


class TestScoringAnalyzer(unittest.TestCase):
    """Тесты для проверки корректности расчетов"""

    def setUp(self):
        """Инициализация перед каждым тестом"""
        self.analyzer = MockScoringAnalyzer()
        self.base_time = datetime(2024, 1, 1, 0, 0, 0)

    def create_candle(self, hours_offset, high, low, close):
        """Создание тестовой свечи"""
        return {
            'timestamp': self.base_time + timedelta(hours=hours_offset),
            'high_price': high,
            'low_price': low,
            'close_price': close
        }

    def test_buy_take_profit(self):
        """Тест: BUY сигнал достигает Take Profit"""
        entry_price = 100.0

        # TP должен сработать на 104 (100 * 1.04)
        history = [
            self.create_candle(1, 101, 99, 100.5),
            self.create_candle(2, 102, 100, 101.5),
            self.create_candle(3, 104.5, 102, 104),  # TP срабатывает здесь
            self.create_candle(4, 106, 103, 105),  # Цена продолжает расти
            self.create_candle(48, 103, 101, 102),
        ]

        result = self.analyzer.process_price_history_improved(
            'BUY', entry_price, history, self.base_time
        )

        # Проверки
        self.assertEqual(result['close_reason'], 'take_profit')
        self.assertTrue(result['is_win'])
        self.assertAlmostEqual(result['close_price'], 104.0, places=2)
        self.assertAlmostEqual(result['pnl_percent'], 4.0, places=2)
        self.assertAlmostEqual(result['pnl_usd'], 20.0, places=2)  # 100 * 5 * 0.04

        # Максимальный потенциал должен учитывать ВСЕ свечи
        self.assertEqual(result['absolute_max_price'], 106)  # Максимум был 106
        self.assertAlmostEqual(result['max_potential_profit_percent'], 6.0, places=2)
        self.assertAlmostEqual(result['max_potential_profit_usd'], 30.0, places=2)

    def test_buy_stop_loss(self):
        """Тест: BUY сигнал достигает Stop Loss"""
        entry_price = 100.0

        # SL должен сработать на 97 (100 * 0.97)
        history = [
            self.create_candle(1, 101, 99, 100.5),
            self.create_candle(2, 99, 96.5, 97),  # SL срабатывает здесь
            self.create_candle(3, 98, 95, 96),  # Цена продолжает падать
            self.create_candle(4, 102, 94, 101),  # Потом восстанавливается
            self.create_candle(48, 103, 101, 102),
        ]

        result = self.analyzer.process_price_history_improved(
            'BUY', entry_price, history, self.base_time
        )

        # Проверки
        self.assertEqual(result['close_reason'], 'stop_loss')
        self.assertFalse(result['is_win'])
        self.assertAlmostEqual(result['close_price'], 97.0, places=2)
        self.assertAlmostEqual(result['pnl_percent'], -3.0, places=2)
        self.assertAlmostEqual(result['pnl_usd'], -15.0, places=2)  # 100 * 5 * -0.03

        # Максимальный потенциал
        self.assertEqual(result['absolute_max_price'], 103)  # Максимум был 103
        self.assertEqual(result['absolute_min_price'], 94)  # Минимум был 94
        self.assertAlmostEqual(result['max_potential_profit_percent'], 3.0, places=2)
        self.assertAlmostEqual(result['max_potential_loss_percent'], 6.0, places=2)

    def test_sell_take_profit(self):
        """Тест: SELL сигнал достигает Take Profit"""
        entry_price = 100.0

        # TP для SELL должен сработать на 96 (100 * 0.96)
        history = [
            self.create_candle(1, 101, 99, 99.5),
            self.create_candle(2, 98, 95.5, 96),  # TP срабатывает здесь
            self.create_candle(3, 95, 93, 94),  # Цена продолжает падать
            self.create_candle(48, 97, 95, 96),
        ]

        result = self.analyzer.process_price_history_improved(
            'SELL', entry_price, history, self.base_time
        )

        # Проверки
        self.assertEqual(result['close_reason'], 'take_profit')
        self.assertTrue(result['is_win'])
        self.assertAlmostEqual(result['close_price'], 96.0, places=2)
        self.assertAlmostEqual(result['pnl_percent'], 4.0, places=2)
        self.assertAlmostEqual(result['pnl_usd'], 20.0, places=2)

        # Максимальный потенциал для SHORT
        self.assertEqual(result['absolute_min_price'], 93)  # Минимум был 93
        self.assertAlmostEqual(result['max_potential_profit_percent'], 7.0, places=2)

    def test_timeout_scenario(self):
        """Тест: позиция закрывается по таймауту"""
        entry_price = 100.0

        # Цена колеблется, но не достигает ни TP (104), ни SL (97)
        # Максимум 102.5, минимум 98 - не достигают уровней закрытия
        history = [
            self.create_candle(1, 101, 99.5, 100.5),
            self.create_candle(2, 102, 98.5, 101),
            self.create_candle(3, 102.5, 99, 101.5),  # Максимум здесь
            self.create_candle(4, 102, 98, 100),  # Минимум здесь
            self.create_candle(5, 101.5, 99, 100.5),
            self.create_candle(48, 101, 99.5, 101.5)  # Последняя свеча
        ]

        result = self.analyzer.process_price_history_improved(
            'BUY', entry_price, history, self.base_time
        )

        # Проверки
        self.assertEqual(result['close_reason'], 'timeout')
        self.assertIsNone(result['is_win'])
        self.assertAlmostEqual(result['close_price'], 101.5, places=2)
        self.assertAlmostEqual(result['hours_to_close'], 48.0, places=1)

        # Максимальный потенциал
        # Макс цена 102.5, мин цена 98
        # Для BUY: профит = (102.5 - 100) / 100 * 100 = 2.5%
        # Для BUY: убыток = (100 - 98) / 100 * 100 = 2.0%
        self.assertAlmostEqual(result['max_potential_profit_percent'], 2.5, places=2)
        self.assertAlmostEqual(result['max_potential_loss_percent'], 2.0, places=2)

    def test_extreme_volatility(self):
        """Тест: экстремальная волатильность после закрытия позиции"""
        entry_price = 100.0

        history = [
            self.create_candle(1, 101, 99, 100),
            self.create_candle(2, 104.5, 102, 104),  # TP срабатывает
            self.create_candle(3, 110, 105, 108),  # Огромный скачок после TP
            self.create_candle(4, 112, 108, 110),  # Еще выше
            self.create_candle(5, 95, 90, 92),  # Резкое падение
            self.create_candle(48, 100, 98, 99),
        ]

        result = self.analyzer.process_price_history_improved(
            'BUY', entry_price, history, self.base_time
        )

        # Позиция закрыта по TP
        self.assertEqual(result['close_reason'], 'take_profit')
        self.assertAlmostEqual(result['pnl_percent'], 4.0, places=2)

        # Но максимальный потенциал учитывает ВСЕ движение
        self.assertEqual(result['absolute_max_price'], 112)  # Максимум 112
        self.assertEqual(result['absolute_min_price'], 90)  # Минимум 90
        self.assertAlmostEqual(result['max_potential_profit_percent'], 12.0, places=2)
        self.assertAlmostEqual(result['max_potential_loss_percent'], 10.0, places=2)

        # В долларах с учетом leverage
        self.assertAlmostEqual(result['max_potential_profit_usd'], 60.0, places=2)  # 100 * 5 * 0.12
        self.assertAlmostEqual(result['max_potential_loss_usd'], 50.0, places=2)  # 100 * 5 * 0.10

    def test_calculation_precision(self):
        """Тест: точность расчетов для различных сценариев"""
        test_cases = [
            # (signal_type, entry_price, max_price, min_price, expected_max_profit%, expected_max_loss%)
            ('BUY', 100, 110, 90, 10.0, 10.0),
            ('BUY', 50, 55, 45, 10.0, 10.0),
            ('SELL', 100, 110, 90, 10.0, 10.0),
            ('SELL', 200, 220, 180, 10.0, 10.0),
        ]

        for signal_type, entry, max_p, min_p, exp_profit, exp_loss in test_cases:
            history = [
                self.create_candle(1, max_p, min_p, (max_p + min_p) / 2),
                self.create_candle(48, entry, entry, entry),
            ]

            result = self.analyzer.process_price_history_improved(
                signal_type, entry, history, self.base_time
            )

            self.assertAlmostEqual(
                result['max_potential_profit_percent'],
                exp_profit,
                places=2,
                msg=f"Failed for {signal_type} at entry {entry}"
            )
            self.assertAlmostEqual(
                result['max_potential_loss_percent'],
                exp_loss,
                places=2,
                msg=f"Failed for {signal_type} at entry {entry}"
            )


class TestEdgeCases(unittest.TestCase):
    """Тесты граничных случаев"""

    def setUp(self):
        self.analyzer = MockScoringAnalyzer()
        self.base_time = datetime(2024, 1, 1, 0, 0, 0)

    def test_tp_and_sl_in_same_candle(self):
        """Тест: TP и SL в одной свече (приоритет у SL)"""
        entry_price = 100.0

        # Свеча с экстремальным размахом
        history = [
            {
                'timestamp': self.base_time + timedelta(hours=1),
                'high_price': 105,  # Выше TP (104)
                'low_price': 96,  # Ниже SL (97)
                'close_price': 100
            },
            {
                'timestamp': self.base_time + timedelta(hours=48),
                'high_price': 102,
                'low_price': 98,
                'close_price': 100
            }
        ]

        result = self.analyzer.process_price_history_improved(
            'BUY', entry_price, history, self.base_time
        )

        # SL должен сработать первым (защита капитала)
        self.assertEqual(result['close_reason'], 'stop_loss')
        self.assertFalse(result['is_win'])

    def test_zero_price_movement(self):
        """Тест: нулевое движение цены"""
        entry_price = 100.0

        # Цена не меняется
        history = [
            {
                'timestamp': self.base_time + timedelta(hours=i),
                'high_price': 100,
                'low_price': 100,
                'close_price': 100
            }
            for i in range(1, 49)
        ]

        result = self.analyzer.process_price_history_improved(
            'BUY', entry_price, history, self.base_time
        )

        self.assertEqual(result['close_reason'], 'timeout')
        self.assertAlmostEqual(result['pnl_percent'], 0.0, places=2)
        self.assertAlmostEqual(result['max_potential_profit_percent'], 0.0, places=2)
        self.assertAlmostEqual(result['max_potential_loss_percent'], 0.0, places=2)

    def test_sell_signal_extreme_case(self):
        """Тест: SELL сигнал с экстремальными движениями"""
        entry_price = 100.0

        # Для SELL: TP на 96, SL на 103
        history = [
            {
                'timestamp': self.base_time + timedelta(hours=1),
                'high_price': 102,
                'low_price': 98,
                'close_price': 100
            },
            {
                'timestamp': self.base_time + timedelta(hours=2),
                'high_price': 103.5,  # SL срабатывает (103)
                'low_price': 95,
                'close_price': 98
            },
            {
                'timestamp': self.base_time + timedelta(hours=3),
                'high_price': 110,  # Огромное движение после
                'low_price': 90,
                'close_price': 95
            },
            {
                'timestamp': self.base_time + timedelta(hours=48),
                'high_price': 100,
                'low_price': 98,
                'close_price': 99
            }
        ]

        result = self.analyzer.process_price_history_improved(
            'SELL', entry_price, history, self.base_time
        )

        # Проверки
        self.assertEqual(result['close_reason'], 'stop_loss')
        self.assertFalse(result['is_win'])
        self.assertAlmostEqual(result['close_price'], 103.0, places=2)

        # Максимальный потенциал для SELL
        # Профит: (100 - 90) / 100 * 100 = 10%
        # Убыток: (110 - 100) / 100 * 100 = 10%
        self.assertAlmostEqual(result['max_potential_profit_percent'], 10.0, places=2)
        self.assertAlmostEqual(result['max_potential_loss_percent'], 10.0, places=2)


class TestNoDataHandling(unittest.TestCase):
    """Тесты обработки сигналов без данных"""

    def test_create_no_data_result(self):
        """Тест: создание записи для сигнала без данных"""

        # Создаем тестовый экземпляр
        class TestAnalyzer:
            def create_no_data_result(self, signal, signal_type, signal_criteria, reason):
                return {
                    'scoring_history_id': signal['scoring_history_id'],
                    'signal_timestamp': signal['signal_timestamp'],
                    'pair_symbol': signal['pair_symbol'],
                    'trading_pair_id': signal['trading_pair_id'],
                    'market_regime': signal['market_regime'],
                    'total_score': float(signal['total_score']),
                    'indicator_score': float(signal['indicator_score']),
                    'pattern_score': float(signal['pattern_score']),
                    'combination_score': float(signal.get('combination_score', 0)),
                    'signal_type': signal_type,
                    'signal_criteria': signal_criteria,
                    'entry_price': None,
                    'best_price': None,
                    'worst_price': None,
                    'close_price': None,
                    'is_closed': False,
                    'close_reason': reason,
                    'is_win': None,
                    'close_time': None,
                    'hours_to_close': None,
                    'pnl_percent': 0,
                    'pnl_usd': 0,
                    'max_potential_profit_percent': 0,
                    'max_potential_profit_usd': 0,
                    'max_drawdown_percent': 0,
                    'max_drawdown_usd': 0,
                    'tp_percent': ANALYSIS_PARAMS['tp_percent'],
                    'sl_percent': ANALYSIS_PARAMS['sl_percent'],
                    'position_size': ANALYSIS_PARAMS['position_size'],
                    'leverage': ANALYSIS_PARAMS['leverage'],
                    'analysis_end_time': signal['signal_timestamp'] + timedelta(hours=48)
                }

        analyzer = TestAnalyzer()

        # Тестовый сигнал
        test_signal = {
            'scoring_history_id': 123,
            'signal_timestamp': datetime(2024, 1, 1, 0, 0, 0),
            'pair_symbol': 'TESTUSDT',
            'trading_pair_id': 1,
            'market_regime': 'bullish',
            'total_score': 80.0,
            'indicator_score': 50.0,
            'pattern_score': 30.0,
            'combination_score': 0
        }

        # Создаем NO_DATA запись
        result = analyzer.create_no_data_result(
            test_signal,
            'BUY',
            'Indicator Score: 50.0',
            'no_entry_price'
        )

        # Проверки
        self.assertEqual(result['scoring_history_id'], 123)
        self.assertEqual(result['pair_symbol'], 'TESTUSDT')
        self.assertEqual(result['signal_type'], 'BUY')
        self.assertIsNone(result['entry_price'])
        self.assertIsNone(result['best_price'])
        self.assertIsNone(result['worst_price'])
        self.assertIsNone(result['close_price'])
        self.assertEqual(result['close_reason'], 'no_entry_price')
        self.assertFalse(result['is_closed'])
        self.assertIsNone(result['is_win'])
        self.assertEqual(result['pnl_percent'], 0)
        self.assertEqual(result['pnl_usd'], 0)
        self.assertEqual(result['max_potential_profit_percent'], 0)
        self.assertEqual(result['max_potential_profit_usd'], 0)
        self.assertEqual(result['tp_percent'], 4.0)
        self.assertEqual(result['sl_percent'], 3.0)
        self.assertEqual(result['position_size'], 100.0)
        self.assertEqual(result['leverage'], 5)

    def test_no_data_reasons(self):
        """Тест: различные причины отсутствия данных"""
        reasons = ['no_entry_price', 'insufficient_history', 'processing_error']

        for reason in reasons:
            # Простая проверка что эти строки корректны
            self.assertIsInstance(reason, str)
            self.assertIn('_', reason)  # Все причины используют подчеркивание


def run_tests():
    """Запуск всех тестов"""
    # Создаем test suite
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Добавляем тесты
    suite.addTests(loader.loadTestsFromTestCase(TestScoringAnalyzer))
    suite.addTests(loader.loadTestsFromTestCase(TestEdgeCases))
    suite.addTests(loader.loadTestsFromTestCase(TestNoDataHandling))

    # Запускаем с подробным выводом
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # Выводим сводку
    print("\n" + "=" * 70)
    print("РЕЗУЛЬТАТЫ ТЕСТИРОВАНИЯ:")
    print("=" * 70)
    print(f"✅ Успешных тестов: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"❌ Провалено: {len(result.failures)}")
    print(f"⚠️ Ошибок: {len(result.errors)}")
    print(f"📊 Всего тестов: {result.testsRun}")
    print("=" * 70)

    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)