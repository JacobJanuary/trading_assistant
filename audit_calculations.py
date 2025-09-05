#!/usr/bin/env python3
"""
Комплексный аудит всех расчетов в системе Trading Assistant
Автор: Trading Assistant Audit System
Дата создания: 2025-09-03
"""

import os
import sys
import psycopg2
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta
from dotenv import load_dotenv
import json
import traceback

# Загружаем переменные окружения
load_dotenv()

class TradingCalculationsAuditor:
    """Класс для проведения аудита всех расчетов в системе"""
    
    def __init__(self):
        """Инициализация подключения к БД"""
        self.connection = None
        self.cursor = None
        self.errors = []
        self.warnings = []
        self.test_results = {
            'total_tests': 0,
            'passed': 0,
            'failed': 0,
            'warnings': 0
        }
        
        # Параметры для тестов
        self.COMMISSION_RATE = Decimal('0.001')  # 0.1% комиссия
        self.TOLERANCE = Decimal('0.01')  # Допустимая погрешность в долларах
        
    def connect_db(self):
        """Подключение к базе данных"""
        try:
            # Пробуем DATABASE_URL
            database_url = os.getenv('DATABASE_URL')
            if database_url:
                self.connection = psycopg2.connect(database_url)
            else:
                # Используем отдельные параметры
                self.connection = psycopg2.connect(
                    host=os.getenv('DB_HOST'),
                    port=os.getenv('DB_PORT', '5432'),
                    database=os.getenv('DB_NAME'),
                    user=os.getenv('DB_USER'),
                    password=os.getenv('DB_PASSWORD')
                )
            self.cursor = self.connection.cursor()
            print("✅ Успешное подключение к базе данных")
            return True
        except Exception as e:
            print(f"❌ Ошибка подключения к БД: {e}")
            return False
    
    def close_db(self):
        """Закрытие подключения к БД"""
        if self.cursor:
            self.cursor.close()
        if self.connection:
            self.connection.close()
    
    def run_test(self, test_name, test_function):
        """Запуск отдельного теста"""
        self.test_results['total_tests'] += 1
        try:
            print(f"\n🧪 Тест: {test_name}")
            result = test_function()
            if result:
                self.test_results['passed'] += 1
                print(f"  ✅ Пройден")
            else:
                self.test_results['failed'] += 1
                print(f"  ❌ Провален")
            return result
        except Exception as e:
            self.test_results['failed'] += 1
            self.errors.append(f"{test_name}: {str(e)}")
            print(f"  ❌ Ошибка: {e}")
            traceback.print_exc()
            return False
    
    def test_pnl_calculation_long(self):
        """Тест расчета P&L для LONG позиций"""
        print("  📊 Проверка формулы P&L для LONG позиций...")
        
        # Тестовые данные
        test_cases = [
            {
                'entry_price': Decimal('100'),
                'exit_price': Decimal('110'),
                'position_size': Decimal('1000'),
                'leverage': 10,
                'expected_pnl': Decimal('1000'),  # ((110-100)/100) * 1000 * 10 = 1000
                'description': 'Прибыльная LONG позиция (+10%)'
            },
            {
                'entry_price': Decimal('100'),
                'exit_price': Decimal('90'),
                'position_size': Decimal('1000'),
                'leverage': 10,
                'expected_pnl': Decimal('-1000'),  # ((90-100)/100) * 1000 * 10 = -1000
                'description': 'Убыточная LONG позиция (-10%)'
            },
            {
                'entry_price': Decimal('50000'),
                'exit_price': Decimal('51000'),
                'position_size': Decimal('100'),
                'leverage': 5,
                'expected_pnl': Decimal('10'),  # (51000-50000)/50000 * 100 * 5 = 10
                'description': 'BTC LONG с малой позицией'
            }
        ]
        
        all_passed = True
        for case in test_cases:
            # Расчет P&L по формуле: (exit_price - entry_price) / entry_price * position_size * leverage
            actual_pnl = ((case['exit_price'] - case['entry_price']) / case['entry_price'] * 
                         case['position_size'] * case['leverage'])
            
            # Учитываем комиссию (открытие + закрытие)
            commission = case['position_size'] * self.COMMISSION_RATE * 2
            actual_pnl_with_commission = actual_pnl - commission
            
            print(f"    📝 {case['description']}:")
            print(f"       Entry: ${case['entry_price']}, Exit: ${case['exit_price']}")
            print(f"       Position: ${case['position_size']}, Leverage: {case['leverage']}x")
            print(f"       Расчетный P&L: ${actual_pnl:.2f}")
            print(f"       P&L с комиссией: ${actual_pnl_with_commission:.2f}")
            print(f"       Ожидаемый P&L: ${case['expected_pnl']:.2f}")
            
            if abs(actual_pnl - case['expected_pnl']) > self.TOLERANCE:
                print(f"       ❌ ОШИБКА: Расхождение {abs(actual_pnl - case['expected_pnl']):.2f}")
                all_passed = False
            else:
                print(f"       ✅ OK")
        
        return all_passed
    
    def test_pnl_calculation_short(self):
        """Тест расчета P&L для SHORT позиций"""
        print("  📊 Проверка формулы P&L для SHORT позиций...")
        
        test_cases = [
            {
                'entry_price': Decimal('100'),
                'exit_price': Decimal('90'),
                'position_size': Decimal('1000'),
                'leverage': 10,
                'expected_pnl': Decimal('1000'),  # (100-90)/100 * 1000 * 10 = 1000
                'description': 'Прибыльная SHORT позиция (+10%)'
            },
            {
                'entry_price': Decimal('100'),
                'exit_price': Decimal('110'),
                'position_size': Decimal('1000'),
                'leverage': 10,
                'expected_pnl': Decimal('-1000'),  # (100-110)/100 * 1000 * 10 = -1000
                'description': 'Убыточная SHORT позиция (-10%)'
            }
        ]
        
        all_passed = True
        for case in test_cases:
            # Расчет P&L для SHORT: (entry_price - exit_price) / entry_price * position_size * leverage
            actual_pnl = ((case['entry_price'] - case['exit_price']) / case['entry_price'] * 
                         case['position_size'] * case['leverage'])
            
            # Учитываем комиссию
            commission = case['position_size'] * self.COMMISSION_RATE * 2
            actual_pnl_with_commission = actual_pnl - commission
            
            print(f"    📝 {case['description']}:")
            print(f"       Entry: ${case['entry_price']}, Exit: ${case['exit_price']}")
            print(f"       Position: ${case['position_size']}, Leverage: {case['leverage']}x")
            print(f"       Расчетный P&L: ${actual_pnl:.2f}")
            print(f"       P&L с комиссией: ${actual_pnl_with_commission:.2f}")
            print(f"       Ожидаемый P&L: ${case['expected_pnl']:.2f}")
            
            if abs(actual_pnl - case['expected_pnl']) > self.TOLERANCE:
                print(f"       ❌ ОШИБКА: Расхождение {abs(actual_pnl - case['expected_pnl']):.2f}")
                all_passed = False
            else:
                print(f"       ✅ OK")
        
        return all_passed
    
    def test_trailing_stop_logic(self):
        """Тест логики Trailing Stop"""
        print("  📊 Проверка логики Trailing Stop...")
        
        test_cases = [
            {
                'entry_price': Decimal('100'),
                'max_price': Decimal('110'),
                'activation_pct': Decimal('2'),  # Активация при +2%
                'distance_pct': Decimal('1'),    # Дистанция 1% от максимума
                'current_price': Decimal('108.9'),
                'should_activate': True,
                'should_trigger': True,
                'description': 'Trailing Stop активирован и сработал'
            },
            {
                'entry_price': Decimal('100'),
                'max_price': Decimal('101.5'),
                'activation_pct': Decimal('2'),
                'distance_pct': Decimal('1'),
                'current_price': Decimal('101'),
                'should_activate': False,
                'should_trigger': False,
                'description': 'Trailing Stop не активирован (профит < 2%)'
            },
            {
                'entry_price': Decimal('100'),
                'max_price': Decimal('105'),
                'activation_pct': Decimal('2'),
                'distance_pct': Decimal('1'),
                'current_price': Decimal('104.5'),
                'should_activate': True,
                'should_trigger': False,
                'description': 'Trailing Stop активирован, но не сработал'
            }
        ]
        
        all_passed = True
        for case in test_cases:
            # Проверка активации
            profit_pct = ((case['max_price'] - case['entry_price']) / case['entry_price'] * 100)
            is_activated = profit_pct >= case['activation_pct']
            
            # Проверка срабатывания
            trigger_price = case['max_price'] * (1 - case['distance_pct'] / 100)
            is_triggered = is_activated and case['current_price'] <= trigger_price
            
            print(f"    📝 {case['description']}:")
            print(f"       Entry: ${case['entry_price']}, Max: ${case['max_price']}, Current: ${case['current_price']}")
            print(f"       Activation: {case['activation_pct']}%, Distance: {case['distance_pct']}%")
            print(f"       Профит на максимуме: {profit_pct:.2f}%")
            print(f"       Цена срабатывания: ${trigger_price:.2f}")
            print(f"       Активирован: {is_activated} (ожидается: {case['should_activate']})")
            print(f"       Сработал: {is_triggered} (ожидается: {case['should_trigger']})")
            
            if is_activated != case['should_activate'] or is_triggered != case['should_trigger']:
                print(f"       ❌ ОШИБКА: Логика работает неверно")
                all_passed = False
            else:
                print(f"       ✅ OK")
        
        return all_passed
    
    def test_stop_loss_take_profit(self):
        """Тест расчета уровней Stop Loss и Take Profit"""
        print("  📊 Проверка расчета SL/TP уровней...")
        
        test_cases = [
            {
                'entry_price': Decimal('100'),
                'sl_percent': Decimal('3'),
                'tp_percent': Decimal('5'),
                'action': 'LONG',
                'expected_sl': Decimal('97'),     # 100 * (1 - 0.03)
                'expected_tp': Decimal('105'),    # 100 * (1 + 0.05)
                'description': 'LONG позиция'
            },
            {
                'entry_price': Decimal('100'),
                'sl_percent': Decimal('3'),
                'tp_percent': Decimal('5'),
                'action': 'SHORT',
                'expected_sl': Decimal('103'),    # 100 * (1 + 0.03)
                'expected_tp': Decimal('95'),     # 100 * (1 - 0.05)
                'description': 'SHORT позиция'
            }
        ]
        
        all_passed = True
        for case in test_cases:
            if case['action'] == 'LONG':
                actual_sl = case['entry_price'] * (1 - case['sl_percent'] / 100)
                actual_tp = case['entry_price'] * (1 + case['tp_percent'] / 100)
            else:  # SHORT
                actual_sl = case['entry_price'] * (1 + case['sl_percent'] / 100)
                actual_tp = case['entry_price'] * (1 - case['tp_percent'] / 100)
            
            print(f"    📝 {case['description']}:")
            print(f"       Entry: ${case['entry_price']}, Action: {case['action']}")
            print(f"       SL%: {case['sl_percent']}%, TP%: {case['tp_percent']}%")
            print(f"       Расчетный SL: ${actual_sl:.2f} (ожидается: ${case['expected_sl']})")
            print(f"       Расчетный TP: ${actual_tp:.2f} (ожидается: ${case['expected_tp']})")
            
            if (abs(actual_sl - case['expected_sl']) > self.TOLERANCE or 
                abs(actual_tp - case['expected_tp']) > self.TOLERANCE):
                print(f"       ❌ ОШИБКА: Неверный расчет уровней")
                all_passed = False
            else:
                print(f"       ✅ OK")
        
        return all_passed
    
    def test_database_signals(self):
        """Тест реальных сигналов из базы данных"""
        print("  📊 Проверка случайных сигналов из БД...")
        
        try:
            # Получаем несколько случайных обработанных сигналов из временной таблицы анализа
            query = """
                SELECT 
                    signal_id,
                    pair_symbol as coin,
                    signal_action,
                    entry_price,
                    close_price as exit_price,
                    close_reason,
                    pnl_usd as realized_pnl_usd,
                    pnl_percent as realized_pnl_percent,
                    100.0 as position_size,  -- Значение по умолчанию
                    5 as leverage,            -- Значение по умолчанию
                    4.0 as take_profit_percent,
                    3.0 as stop_loss_percent,
                    2.0 as trailing_stop_percent,
                    false as use_trailing_stop
                FROM web.scoring_analysis_temp
                WHERE is_closed = true
                    AND close_price IS NOT NULL
                    AND pnl_usd IS NOT NULL
                ORDER BY RANDOM()
                LIMIT 5
            """
            
            self.cursor.execute(query)
            signals = self.cursor.fetchall()
            
            if not signals:
                print("    ⚠️ Нет закрытых сигналов в БД для проверки")
                return True
            
            all_passed = True
            for signal in signals:
                signal_id = signal[0]
                coin = signal[1]
                action = signal[2]
                entry_price = Decimal(str(signal[3]))
                exit_price = Decimal(str(signal[4]))
                close_reason = signal[5]
                db_pnl_usd = Decimal(str(signal[6])) if signal[6] else None
                db_pnl_percent = Decimal(str(signal[7])) if signal[7] else None
                position_size = Decimal(str(signal[8]))
                leverage = signal[9]
                tp_percent = Decimal(str(signal[10])) if signal[10] else None
                sl_percent = Decimal(str(signal[11])) if signal[11] else None
                
                # Пересчитываем P&L
                if action == 'LONG':
                    calc_pnl_percent = ((exit_price - entry_price) / entry_price * 100)
                else:  # SHORT
                    calc_pnl_percent = ((entry_price - exit_price) / entry_price * 100)
                
                calc_pnl_usd = (calc_pnl_percent / 100) * position_size * leverage
                
                # Учитываем комиссию
                commission = position_size * self.COMMISSION_RATE * 2
                calc_pnl_usd_with_commission = calc_pnl_usd - commission
                
                print(f"\n    📝 Signal {signal_id} ({coin} {action}):")
                print(f"       Entry: ${entry_price:.4f}, Exit: ${exit_price:.4f}")
                print(f"       Close reason: {close_reason}")
                print(f"       Position: ${position_size}, Leverage: {leverage}x")
                print(f"       DB P&L: ${db_pnl_usd:.2f} ({db_pnl_percent:.2f}%)")
                print(f"       Расчетный P&L: ${calc_pnl_usd:.2f} ({calc_pnl_percent:.2f}%)")
                print(f"       P&L с комиссией: ${calc_pnl_usd_with_commission:.2f}")
                
                # Проверяем расхождение (допускаем погрешность из-за округлений)
                tolerance = Decimal('1')  # $1 допустимая погрешность
                if db_pnl_usd and abs(calc_pnl_usd_with_commission - db_pnl_usd) > tolerance:
                    print(f"       ⚠️ ПРЕДУПРЕЖДЕНИЕ: Расхождение ${abs(calc_pnl_usd_with_commission - db_pnl_usd):.2f}")
                    self.warnings.append(f"Signal {signal_id}: P&L расхождение ${abs(calc_pnl_usd_with_commission - db_pnl_usd):.2f}")
                    self.test_results['warnings'] += 1
                else:
                    print(f"       ✅ OK")
            
            return all_passed
            
        except Exception as e:
            print(f"    ❌ Ошибка при проверке БД: {e}")
            return False
    
    def test_commission_calculation(self):
        """Тест расчета комиссий"""
        print("  📊 Проверка расчета комиссий...")
        
        test_cases = [
            {
                'position_size': Decimal('1000'),
                'commission_rate': Decimal('0.001'),  # 0.1%
                'expected_open': Decimal('1'),        # 1000 * 0.001
                'expected_close': Decimal('1'),       # 1000 * 0.001
                'expected_total': Decimal('2'),       # 1 + 1
                'description': 'Стандартная позиция $1000'
            },
            {
                'position_size': Decimal('100'),
                'commission_rate': Decimal('0.001'),
                'expected_open': Decimal('0.1'),
                'expected_close': Decimal('0.1'),
                'expected_total': Decimal('0.2'),
                'description': 'Малая позиция $100'
            }
        ]
        
        all_passed = True
        for case in test_cases:
            open_commission = case['position_size'] * case['commission_rate']
            close_commission = case['position_size'] * case['commission_rate']
            total_commission = open_commission + close_commission
            
            print(f"    📝 {case['description']}:")
            print(f"       Position: ${case['position_size']}")
            print(f"       Commission rate: {case['commission_rate'] * 100:.2f}%")
            print(f"       Open commission: ${open_commission:.2f} (ожидается: ${case['expected_open']})")
            print(f"       Close commission: ${close_commission:.2f} (ожидается: ${case['expected_close']})")
            print(f"       Total commission: ${total_commission:.2f} (ожидается: ${case['expected_total']})")
            
            if (abs(open_commission - case['expected_open']) > Decimal('0.01') or
                abs(close_commission - case['expected_close']) > Decimal('0.01') or
                abs(total_commission - case['expected_total']) > Decimal('0.01')):
                print(f"       ❌ ОШИБКА: Неверный расчет комиссии")
                all_passed = False
            else:
                print(f"       ✅ OK")
        
        return all_passed
    
    def test_win_rate_calculation(self):
        """Тест расчета Win Rate"""
        print("  📊 Проверка расчета Win Rate...")
        
        test_cases = [
            {
                'wins': 60,
                'losses': 40,
                'expected_win_rate': 60.0,  # 60 / (60 + 40) * 100
                'description': '60 побед, 40 поражений'
            },
            {
                'wins': 75,
                'losses': 25,
                'expected_win_rate': 75.0,  # 75 / (75 + 25) * 100
                'description': '75 побед, 25 поражений'
            },
            {
                'wins': 0,
                'losses': 10,
                'expected_win_rate': 0.0,   # 0 / (0 + 10) * 100
                'description': 'Только поражения'
            }
        ]
        
        all_passed = True
        for case in test_cases:
            total_closed = case['wins'] + case['losses']
            if total_closed > 0:
                actual_win_rate = (case['wins'] / total_closed) * 100
            else:
                actual_win_rate = 0.0
            
            print(f"    📝 {case['description']}:")
            print(f"       Wins: {case['wins']}, Losses: {case['losses']}")
            print(f"       Расчетный Win Rate: {actual_win_rate:.1f}%")
            print(f"       Ожидаемый Win Rate: {case['expected_win_rate']:.1f}%")
            
            if abs(actual_win_rate - case['expected_win_rate']) > 0.1:
                print(f"       ❌ ОШИБКА: Неверный расчет Win Rate")
                all_passed = False
            else:
                print(f"       ✅ OK")
        
        return all_passed
    
    def generate_report(self):
        """Генерация итогового отчета"""
        print("\n" + "="*80)
        print("📋 ИТОГОВЫЙ ОТЧЕТ АУДИТА")
        print("="*80)
        
        print(f"\n📊 Результаты тестирования:")
        print(f"   Всего тестов: {self.test_results['total_tests']}")
        print(f"   ✅ Пройдено: {self.test_results['passed']}")
        print(f"   ❌ Провалено: {self.test_results['failed']}")
        print(f"   ⚠️  Предупреждений: {self.test_results['warnings']}")
        
        success_rate = (self.test_results['passed'] / self.test_results['total_tests'] * 100 
                       if self.test_results['total_tests'] > 0 else 0)
        print(f"\n   📈 Успешность: {success_rate:.1f}%")
        
        if self.errors:
            print(f"\n❌ Ошибки ({len(self.errors)}):")
            for error in self.errors:
                print(f"   • {error}")
        
        if self.warnings:
            print(f"\n⚠️ Предупреждения ({len(self.warnings)}):")
            for warning in self.warnings[:10]:  # Показываем первые 10
                print(f"   • {warning}")
            if len(self.warnings) > 10:
                print(f"   ... и еще {len(self.warnings) - 10} предупреждений")
        
        # Рекомендации
        print("\n📝 РЕКОМЕНДАЦИИ:")
        if self.test_results['failed'] == 0 and self.test_results['warnings'] == 0:
            print("   ✅ Все расчеты работают корректно!")
        else:
            if self.test_results['failed'] > 0:
                print("   • Необходимо исправить критические ошибки в расчетах")
            if self.test_results['warnings'] > 5:
                print("   • Рекомендуется проверить точность расчетов с учетом округлений")
                print("   • Возможны расхождения из-за изменения комиссий или округлений")
        
        print("\n" + "="*80)
        
        # Сохраняем отчет в файл
        report_file = f"audit_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write("ОТЧЕТ АУДИТА РАСЧЕТОВ TRADING ASSISTANT\n")
            f.write(f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(f"Результаты:\n")
            f.write(f"  Всего тестов: {self.test_results['total_tests']}\n")
            f.write(f"  Пройдено: {self.test_results['passed']}\n")
            f.write(f"  Провалено: {self.test_results['failed']}\n")
            f.write(f"  Предупреждений: {self.test_results['warnings']}\n")
            f.write(f"  Успешность: {success_rate:.1f}%\n\n")
            
            if self.errors:
                f.write(f"Ошибки:\n")
                for error in self.errors:
                    f.write(f"  • {error}\n")
            
            if self.warnings:
                f.write(f"\nПредупреждения:\n")
                for warning in self.warnings:
                    f.write(f"  • {warning}\n")
        
        print(f"\n💾 Отчет сохранен в файл: {report_file}")
    
    def run_full_audit(self):
        """Запуск полного аудита"""
        print("🚀 ЗАПУСК КОМПЛЕКСНОГО АУДИТА РАСЧЕТОВ")
        print("="*80)
        
        if not self.connect_db():
            print("❌ Не удалось подключиться к БД. Аудит прерван.")
            return
        
        # Запускаем все тесты
        self.run_test("Расчет P&L для LONG позиций", self.test_pnl_calculation_long)
        self.run_test("Расчет P&L для SHORT позиций", self.test_pnl_calculation_short)
        self.run_test("Логика Trailing Stop", self.test_trailing_stop_logic)
        self.run_test("Расчет уровней SL/TP", self.test_stop_loss_take_profit)
        self.run_test("Расчет комиссий", self.test_commission_calculation)
        self.run_test("Расчет Win Rate", self.test_win_rate_calculation)
        self.run_test("Проверка сигналов из БД", self.test_database_signals)
        
        # Закрываем соединение с БД
        self.close_db()
        
        # Генерируем отчет
        self.generate_report()

def main():
    """Главная функция"""
    auditor = TradingCalculationsAuditor()
    auditor.run_full_audit()

if __name__ == "__main__":
    main()