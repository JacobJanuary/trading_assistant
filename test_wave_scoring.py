#!/usr/bin/env python3
"""
Тест для process_scoring_signals_batch_v2
Проверяет работу wave-based scoring analysis
"""

from datetime import datetime, timedelta
from trading_simulation import TradingSimulation
from database import group_signals_by_wave

def test_wave_based_scoring():
    """Тест базовой функциональности wave-based scoring"""

    print("\n===== ТЕСТ: Wave-based Scoring Analysis =====\n")

    # Создаем тестовые сигналы
    base_time = datetime(2025, 10, 6, 10, 0, 0)

    test_signals = [
        {
            'signal_id': 1,
            'timestamp': base_time,
            'pair_symbol': 'BTCUSDT',
            'trading_pair_id': 100,
            'signal_action': 'BUY',
            'score_week': 92.0,
            'score_month': 85.0,
            'market_regime': 'BULLISH',
            'exchange_name': 'Binance'
        },
        {
            'signal_id': 2,
            'timestamp': base_time + timedelta(minutes=5),
            'pair_symbol': 'ETHUSDT',
            'trading_pair_id': 101,
            'signal_action': 'SELL',
            'score_week': 88.0,
            'score_month': 82.0,
            'market_regime': 'BEARISH',
            'exchange_name': 'Bybit'
        },
        {
            'signal_id': 3,
            'timestamp': base_time + timedelta(minutes=15),  # Новая волна
            'pair_symbol': 'SOLUSDT',
            'trading_pair_id': 102,
            'signal_action': 'BUY',
            'score_week': 95.0,
            'score_month': 90.0,
            'market_regime': 'BULLISH',
            'exchange_name': 'Binance'
        },
        {
            'signal_id': 4,
            'timestamp': base_time + timedelta(minutes=15),  # Та же волна
            'pair_symbol': 'ADAUSDT',
            'trading_pair_id': 103,
            'signal_action': 'BUY',
            'score_week': 78.0,
            'score_month': 75.0,
            'market_regime': 'NEUTRAL',
            'exchange_name': 'Binance'
        },
    ]

    # Тест 1: Группировка по волнам
    print("1. Тест группировки по волнам...")
    signals_by_wave = group_signals_by_wave(test_signals, wave_interval_minutes=15)

    print(f"   Сигналов: {len(test_signals)}")
    print(f"   Волн: {len(signals_by_wave)}")

    assert len(signals_by_wave) == 2, f"Ожидалось 2 волны, получено {len(signals_by_wave)}"

    # Проверяем количество сигналов в каждой волне
    wave_times = sorted(signals_by_wave.keys())
    wave1_signals = signals_by_wave[wave_times[0]]
    wave2_signals = signals_by_wave[wave_times[1]]

    print(f"   Волна 1 ({wave_times[0]}): {len(wave1_signals)} сигналов")
    print(f"   Волна 2 ({wave_times[1]}): {len(wave2_signals)} сигналов")

    assert len(wave1_signals) == 2, f"В волне 1 ожидалось 2 сигнала, получено {len(wave1_signals)}"
    assert len(wave2_signals) == 2, f"В волне 2 ожидалось 2 сигнала, получено {len(wave2_signals)}"

    # Проверяем сортировку по score_week
    print(f"   Волна 1 score_week: {[s['score_week'] for s in wave1_signals]}")
    print(f"   Волна 2 score_week: {[s['score_week'] for s in wave2_signals]}")

    assert wave1_signals[0]['score_week'] == 92.0, "Первый сигнал волны 1 должен иметь score_week=92"
    assert wave2_signals[0]['score_week'] == 95.0, "Первый сигнал волны 2 должен иметь score_week=95"

    print("   ✓ Группировка работает корректно\n")

    # Тест 2: TradingSimulation - управление капиталом
    print("2. Тест TradingSimulation - управление капиталом...")

    sim = TradingSimulation(
        initial_capital=1000.0,
        position_size=200.0,
        leverage=10,
        tp_percent=4.0,
        sl_percent=3.0,
        use_trailing_stop=False
    )

    print(f"   Начальный капитал: ${sim.initial_capital}")
    print(f"   Доступный капитал: ${sim.available_capital}")
    print(f"   Размер позиции: ${sim.position_size}")

    # Проверяем возможность открытия позиции
    can_open, reason = sim.can_open_position('BTCUSDT')
    assert can_open, f"Должна быть возможность открыть позицию: {reason}"
    print(f"   ✓ Можно открыть позицию (причина: {reason})\n")

    # Тест 3: Проверка дубликатов
    print("3. Тест проверки дубликатов...")

    # Добавляем позицию в open_positions вручную
    sim.open_positions['BTCUSDT'] = {
        'signal_id': 1,
        'entry_price': 50000,
        'position_size': 200,
        'is_closed': False
    }

    can_open, reason = sim.can_open_position('BTCUSDT')
    assert not can_open, "Не должна быть возможность открыть дубликат"
    assert reason == 'duplicate_pair', f"Причина должна быть duplicate_pair, получено: {reason}"
    print(f"   ✓ Дубликаты корректно блокируются (причина: {reason})\n")

    # Тест 4: Резервирование капитала
    print("4. Тест резервирования капитала...")

    sim2 = TradingSimulation(
        initial_capital=500.0,
        position_size=200.0,
        leverage=10,
        tp_percent=4.0,
        sl_percent=3.0
    )

    # Можем открыть 2 позиции (500 / 200 = 2.5)
    can_open, reason = sim2.can_open_position('PAIR1')
    assert can_open, "Должна быть возможность открыть первую позицию"
    sim2.available_capital -= sim2.position_size  # Резервируем
    print(f"   После открытия 1 позиции: ${sim2.available_capital}")

    can_open, reason = sim2.can_open_position('PAIR2')
    assert can_open, "Должна быть возможность открыть вторую позицию"
    sim2.available_capital -= sim2.position_size  # Резервируем
    print(f"   После открытия 2 позиции: ${sim2.available_capital}")

    can_open, reason = sim2.can_open_position('PAIR3')
    assert not can_open, "Не должна быть возможность открыть третью позицию"
    assert reason == 'insufficient_capital', f"Причина должна быть insufficient_capital, получено: {reason}"
    print(f"   ✓ Капитал корректно резервируется (осталось: ${sim2.available_capital})\n")

    # Тест 5: Статистика
    print("5. Тест статистики...")

    sim3 = TradingSimulation(
        initial_capital=1000.0,
        position_size=200.0,
        leverage=10,
        tp_percent=4.0,
        sl_percent=3.0
    )

    summary = sim3.get_summary()

    assert summary['initial_capital'] == 1000.0
    assert summary['final_equity'] == 1000.0  # Пока нет сделок
    assert summary['total_pnl'] == 0.0
    assert summary['max_concurrent_positions'] == 0
    assert summary['total_trades'] == 0
    assert summary['win_rate'] == 0

    print(f"   Initial Capital: ${summary['initial_capital']}")
    print(f"   Final Equity: ${summary['final_equity']}")
    print(f"   Total PnL: ${summary['total_pnl']}")
    print(f"   Win Rate: {summary['win_rate']}%")
    print(f"   ✓ Статистика корректна\n")

    print("===== ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО =====\n")
    return True


if __name__ == '__main__':
    try:
        test_wave_based_scoring()
    except AssertionError as e:
        print(f"\n❌ ТЕСТ ПРОВАЛЕН: {e}\n")
        exit(1)
    except Exception as e:
        print(f"\n❌ ОШИБКА: {e}\n")
        import traceback
        traceback.print_exc()
        exit(1)
