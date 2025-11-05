"""
Класс для симуляции торговли с управлением капиталом и отслеживанием позиций
Используется для wave-based scoring analysis
"""

from datetime import timedelta
from collections import defaultdict
from config import Config
import math

# Константа для slippage на stop-loss
SLIPPAGE_PERCENT = 0.05  # 0.05% проскальзывание на stop-loss


class TradingSimulation:
    """
    Управляет симуляцией торговли с:
    - Управлением капиталом (capital management)
    - Отслеживанием открытых позиций (position tracking)
    - Расчетом метрик (metrics calculation)
    """

    def __init__(self, initial_capital, position_size, leverage,
                 tp_percent, sl_percent,
                 use_trailing_stop=False,
                 trailing_distance_pct=None,
                 trailing_activation_pct=None):
        """
        Инициализация симуляции

        Args:
            initial_capital: Начальный капитал (например, $1000)
            position_size: Размер позиции (маржа) (например, $200)
            leverage: Плечо (например, 10x)
            tp_percent: Take Profit %
            sl_percent: Stop Loss %
            use_trailing_stop: Использовать Trailing Stop
            trailing_distance_pct: Distance % для TS
            trailing_activation_pct: Activation % для TS
        """
        # Капитал
        self.initial_capital = initial_capital
        self.available_capital = initial_capital
        self.position_size = position_size
        self.leverage = leverage

        # Параметры торговли
        self.tp_percent = tp_percent
        self.sl_percent = sl_percent
        self.use_trailing_stop = use_trailing_stop
        self.trailing_distance_pct = trailing_distance_pct or Config.DEFAULT_TRAILING_DISTANCE_PCT
        self.trailing_activation_pct = trailing_activation_pct or Config.DEFAULT_TRAILING_ACTIVATION_PCT

        # Открытые позиции (key = pair_symbol, value = position_info)
        self.open_positions = {}

        # Закрытые сделки
        self.closed_trades = []

        # Метрики
        self.total_pnl = 0.0
        self.total_commission_paid = 0.0
        self.max_concurrent_positions = 0
        self.min_equity = initial_capital

        # Статистика
        self.stats = {
            'total_signals_processed': 0,
            'trades_opened': 0,
            'trades_closed': 0,
            'skipped_no_capital': 0,
            'skipped_duplicate': 0,
            'skipped_wave_limit': 0,
        }

    def cap_loss_to_margin(self, gross_pnl, entry_commission, exit_commission):
        """
        Ограничивает убыток размером изолированной маржи

        При isolated margin максимальный убыток = начальная маржа + все комиссии
        Начальная маржа = position_size (не умноженная на leverage)
        Максимальный убыток = -(position_size + entry_commission + exit_commission)

        Args:
            gross_pnl: Валовая прибыль/убыток до комиссий
            entry_commission: Комиссия при входе
            exit_commission: Комиссия при выходе

        Returns:
            float: Чистый PnL, ограниченный размером маржи
        """
        # Общие комиссии
        total_commission = entry_commission + exit_commission

        # Чистый PnL после комиссий
        net_pnl = gross_pnl - total_commission

        # КРИТИЧНО: При isolated margin максимальный убыток = начальная маржа + все комиссии
        max_loss = -(self.position_size + entry_commission + exit_commission)

        # Возвращаем большее из двух значений (ограничение убытка)
        if net_pnl < max_loss:
            print(f"[ISOLATED MARGIN CAP] Capping loss from ${net_pnl:.2f} to ${max_loss:.2f} (position: ${self.position_size}, fees: ${total_commission:.2f})")
        return max(net_pnl, max_loss)

    def can_open_position(self, pair_symbol):
        """
        Проверка возможности открытия новой позиции

        Returns:
            (bool, str): (можно ли открыть, причина если нельзя)
        """
        # Проверка капитала
        if self.available_capital < self.position_size:
            return False, 'insufficient_capital'

        # Проверка дубликата по паре
        if pair_symbol in self.open_positions:
            return False, 'duplicate_pair'

        return True, 'ok'

    def open_position(self, signal, entry_price, market_data, simulation_end_time=None):
        """
        Открытие новой позиции

        Args:
            signal: Словарь с информацией о сигнале
            entry_price: Цена входа
            market_data: История цен для симуляции
            simulation_end_time: Время окончания симуляции (для принудительного закрытия)

        Returns:
            dict: Информация о результате открытия
        """
        pair_symbol = signal['pair_symbol']
        signal_action = signal['signal_action']
        signal_timestamp = signal['timestamp']

        # Проверяем возможность открытия
        can_open, reason = self.can_open_position(pair_symbol)
        if not can_open:
            self.stats['skipped_' + reason] += 1
            return {'success': False, 'reason': reason}

        # Резервируем капитал
        self.available_capital -= self.position_size

        # Симулируем сделку с помощью существующей логики
        if self.use_trailing_stop:
            from database import calculate_trailing_stop_exit
            result = calculate_trailing_stop_exit(
                entry_price, market_data, signal_action,
                self.trailing_distance_pct, self.trailing_activation_pct,
                self.sl_percent, self.position_size, self.leverage,
                signal_timestamp, Config.DEFAULT_COMMISSION_RATE,
                simulation_end_time=simulation_end_time
            )
        else:
            # Fixed TP/SL логика (упрощенно вызываем)
            result = self._simulate_fixed_tp_sl(
                entry_price, market_data, signal_action, signal_timestamp,
                simulation_end_time=simulation_end_time
            )

        # Создаем информацию о позиции
        position_info = {
            'signal_id': signal['signal_id'],
            'pair_symbol': pair_symbol,
            'signal_action': signal_action,
            'entry_price': entry_price,
            'entry_time': signal_timestamp,
            'position_size': self.position_size,
            'leverage': self.leverage,
            'simulation_result': result,
            'is_closed': result.get('is_closed', False),
            'close_time': result.get('close_time'),
            'close_price': result.get('close_price'),
            'close_reason': result.get('close_reason'),
            'pnl_usd': result.get('pnl_usd', 0),
        }

        # Если позиция сразу не закрылась, добавляем в открытые
        if not position_info['is_closed']:
            self.open_positions[pair_symbol] = position_info
        else:
            # Если закрылась сразу, освобождаем капитал и учитываем PnL
            self._close_position_internal(position_info)

        # Обновляем статистику
        self.stats['trades_opened'] += 1
        self.max_concurrent_positions = max(self.max_concurrent_positions, len(self.open_positions))

        return {'success': True, 'position': position_info}

    def _simulate_fixed_tp_sl(self, entry_price, history, signal_action, signal_timestamp, simulation_end_time=None):
        """
        Упрощенная симуляция Fixed TP/SL

        Args:
            simulation_end_time: Время окончания симуляции (для принудительного закрытия по period_end)
        """
        from config import Config

        is_long = signal_action in ['BUY', 'LONG']
        is_short = signal_action in ['SELL', 'SHORT']

        # Расчет комиссий
        commission_rate = Config.DEFAULT_COMMISSION_RATE
        effective_position = self.position_size * self.leverage
        entry_commission = effective_position * commission_rate
        exit_commission = effective_position * commission_rate
        total_commission = entry_commission + exit_commission

        # Уровни TP/SL
        if is_short:
            tp_price = entry_price * (1 - self.tp_percent / 100)
            sl_price = entry_price * (1 + self.sl_percent / 100)
        else:
            tp_price = entry_price * (1 + self.tp_percent / 100)
            sl_price = entry_price * (1 - self.sl_percent / 100)

        # Проходим по истории
        is_closed = False
        close_price = None
        close_time = None
        close_reason = None
        max_profit_usd = 0
        best_price = entry_price

        liquidation_threshold = Config.LIQUIDATION_THRESHOLD
        liquidation_loss_pct = -(100 / self.leverage) * liquidation_threshold

        for candle in history:
            current_time = candle['timestamp']
            high_price = float(candle['high_price'])
            low_price = float(candle['low_price'])

            # Обновляем best price для max profit
            if is_short:
                if low_price < best_price:
                    best_price = low_price
                    max_profit_percent = ((entry_price - best_price) / entry_price) * 100
                    gross_max_profit = effective_position * (max_profit_percent / 100)
                    max_profit_usd = gross_max_profit - total_commission
            else:
                if high_price > best_price:
                    best_price = high_price
                    max_profit_percent = ((best_price - entry_price) / entry_price) * 100
                    gross_max_profit = effective_position * (max_profit_percent / 100)
                    max_profit_usd = gross_max_profit - total_commission

            # Проверка закрытия
            if not is_closed:
                # КРИТИЧНО: Проверка simulation_end_time (как в check_wr_final.py:316-318)
                if simulation_end_time and current_time >= simulation_end_time:
                    is_closed = True
                    close_reason = 'period_end'
                    close_price = float(candle['close_price'])
                    close_time = current_time
                    break

                # Ликвидация
                if is_long:
                    unrealized_pnl_pct = ((low_price - entry_price) / entry_price) * 100
                else:
                    unrealized_pnl_pct = ((entry_price - high_price) / entry_price) * 100

                if unrealized_pnl_pct <= liquidation_loss_pct:
                    is_closed = True
                    close_reason = 'liquidation'
                    close_price = low_price if is_long else high_price
                    close_time = current_time
                    break

                # TP/SL
                if is_short:
                    if low_price <= tp_price:
                        is_closed = True
                        close_reason = 'take_profit'
                        close_price = tp_price
                        close_time = current_time
                    elif high_price >= sl_price:
                        is_closed = True
                        close_reason = 'stop_loss'
                        # Применяем slippage (исполнение хуже на 0.05%)
                        close_price = sl_price * (1 + SLIPPAGE_PERCENT / 100)
                        print(f"[SLIPPAGE SHORT] SL at {sl_price:.4f}, executed at {close_price:.4f}")
                        close_time = current_time
                else:  # LONG
                    if high_price >= tp_price:
                        is_closed = True
                        close_reason = 'take_profit'
                        close_price = tp_price
                        close_time = current_time
                    elif low_price <= sl_price:
                        is_closed = True
                        close_reason = 'stop_loss'
                        # Применяем slippage (исполнение хуже на 0.05%)
                        close_price = sl_price * (1 - SLIPPAGE_PERCENT / 100)
                        print(f"[SLIPPAGE LONG] SL at {sl_price:.4f}, executed at {close_price:.4f}")
                        close_time = current_time

                if is_closed:
                    break

        # Если позиция все еще открыта после цикла - проверяем data_end (как в check_wr_final.py:321-323)
        if not is_closed and len(history) > 0:
            last_candle = history[-1]
            last_time = last_candle['timestamp']
            last_price = float(last_candle['close_price'])

            # Закрываем с data_end
            is_closed = True
            close_reason = 'data_end'
            close_price = last_price
            close_time = last_time

        # Расчет PnL
        pnl_usd = 0
        if is_closed:
            if is_short:
                pnl_percent = ((entry_price - close_price) / entry_price) * 100
            else:
                pnl_percent = ((close_price - entry_price) / entry_price) * 100

            gross_pnl = effective_position * (pnl_percent / 100)

            # Применяем ограничение isolated margin
            pnl_usd = self.cap_loss_to_margin(gross_pnl, entry_commission, exit_commission)

            # Логирование для отладки (опционально)
            if pnl_usd < gross_pnl - total_commission:
                print(f"[CAP APPLIED] Original loss: {gross_pnl - total_commission:.2f}, "
                      f"Capped to: {pnl_usd:.2f}")

        # Расчет PnL percent для возврата
        pnl_percent = 0
        if is_closed and close_price:
            if is_short:
                pnl_percent = ((entry_price - close_price) / entry_price) * 100
            else:
                pnl_percent = ((close_price - entry_price) / entry_price) * 100

        # Расчет max_profit_percent
        max_profit_percent = 0
        if best_price != entry_price:
            if is_short:
                max_profit_percent = ((entry_price - best_price) / entry_price) * 100
            else:
                max_profit_percent = ((best_price - entry_price) / entry_price) * 100

        return {
            'is_closed': is_closed,
            'close_price': close_price,
            'close_time': close_time,
            'close_reason': close_reason,
            'pnl_usd': pnl_usd,
            'pnl_percent': pnl_percent,
            'max_profit_usd': max_profit_usd,
            'max_profit_percent': max_profit_percent,
            'best_price': best_price,
            'commission_usd': total_commission if is_closed else 0,
            'history': history,  # Нужно для force_close_all_positions
            'entry_price': entry_price,  # Нужно для force_close_all_positions
        }

    def _close_position_internal(self, position_info):
        """Внутренняя функция закрытия позиции (освобождение капитала, учет PnL)"""
        # PnL уже должен быть ограничен, но проверяем на всякий случай
        if 'pnl_usd' in position_info and position_info['pnl_usd'] is not None:
            entry_commission = position_info.get('entry_commission',
                                                self.position_size * self.leverage * 0.0006)
            exit_commission = self.position_size * self.leverage * 0.0006

            # Дополнительная проверка isolated margin ограничения
            # КРИТИЧНО: max_loss = -(position_size + entry_commission + exit_commission)
            max_loss = -(self.position_size + entry_commission + exit_commission)
            actual_pnl = max(position_info['pnl_usd'], max_loss)

            if actual_pnl != position_info['pnl_usd']:
                print(f"[CLOSE DUE CAP] Adjusting PnL from {position_info['pnl_usd']:.2f} "
                      f"to {actual_pnl:.2f}")

            self.total_pnl += actual_pnl
            # Освобождаем капитал и добавляем PnL
            self.available_capital += self.position_size + actual_pnl
        else:
            # Если PnL не определен, просто возвращаем капитал
            self.available_capital += self.position_size

        # Учитываем комиссии
        commission = position_info['simulation_result'].get('commission_usd', 0)
        self.total_commission_paid += commission

        # Добавляем в закрытые сделки
        self.closed_trades.append(position_info)
        self.stats['trades_closed'] += 1

    def close_due_positions(self, wave_time):
        """
        Закрывает позиции, у которых close_time <= wave_time

        Args:
            wave_time: Время текущей волны

        Returns:
            list: Список закрытых пар
        """
        closed_pairs = []

        for pair, position in list(self.open_positions.items()):
            if position['close_time'] and position['close_time'] <= wave_time:
                self._close_position_internal(position)
                closed_pairs.append(pair)

        # Удаляем закрытые позиции
        for pair in closed_pairs:
            del self.open_positions[pair]

        return closed_pairs

    def update_equity_metrics(self, wave_time, market_data_by_pair=None):
        """
        Обновляет метрики equity с учетом floating PnL открытых позиций

        Args:
            wave_time: Время текущей волны
            market_data_by_pair: Словарь {pair_symbol: market_data} для расчета текущих цен
        """
        floating_pnl = 0.0

        # Если есть открытые позиции и переданы рыночные данные
        if self.open_positions and market_data_by_pair:
            for pair, position in self.open_positions.items():
                # Получаем текущую цену из market_data
                if pair in market_data_by_pair:
                    history = market_data_by_pair[pair]
                    # Находим свечу на текущее время волны
                    current_price = None
                    for candle in history:
                        if candle['timestamp'] <= wave_time:
                            current_price = float(candle['close_price'])
                        else:
                            break

                    if current_price:
                        # Рассчитываем unrealized PnL
                        entry_price = position['entry_price']
                        is_long = position['signal_action'] in ['BUY', 'LONG']

                        if is_long:
                            unrealized_pnl_percent = ((current_price - entry_price) / entry_price) * 100
                        else:
                            unrealized_pnl_percent = ((entry_price - current_price) / entry_price) * 100

                        effective_position = self.position_size * self.leverage
                        unrealized_pnl = effective_position * (unrealized_pnl_percent / 100)

                        # Ограничиваем floating убыток 95% маржи
                        # (5% резерв так как позиция еще не закрыта)
                        max_floating_loss = -self.position_size * 0.95
                        if unrealized_pnl < max_floating_loss:
                            print(f"[FLOATING CAP] {pair}: Capping from {unrealized_pnl:.2f} "
                                  f"to {max_floating_loss:.2f}")
                            unrealized_pnl = max_floating_loss

                        floating_pnl += unrealized_pnl

        # Рассчитываем текущий equity
        # equity = available_capital + floating_pnl + (locked_capital)
        locked_capital = len(self.open_positions) * self.position_size
        current_equity = self.available_capital + floating_pnl + locked_capital

        # Обновляем min_equity
        self.min_equity = min(self.min_equity, current_equity)

    def force_close_all_positions(self, simulation_end_time):
        """
        Принудительно закрывает все открытые позиции в конце симуляции
        ВАЖНО: Находит последнюю цену из истории и пересчитывает PnL

        Args:
            simulation_end_time: Время окончания симуляции
        """
        for pair, position in list(self.open_positions.items()):
            # Если позиция еще не закрыта, закрываем принудительно
            if not position['is_closed']:
                result = position['simulation_result']
                history = result.get('history', [])
                entry_price = result.get('entry_price', 0)
                is_long = position['signal_action'].upper() in ['BUY', 'LONG']

                # Находим последнюю цену ДО конца периода (как в check_wr_final.py:491-497)
                last_price = None
                for candle in history:
                    if candle['timestamp'] <= simulation_end_time:
                        last_price = float(candle['close_price'])
                    else:
                        break

                if last_price and entry_price > 0:
                    # Пересчитываем PnL (как в check_wr_final.py:529-542)
                    effective_position = self.position_size * self.leverage

                    if is_long:
                        pnl_percent = ((last_price - entry_price) / entry_price) * 100
                    else:
                        pnl_percent = ((entry_price - last_price) / entry_price) * 100

                    gross_pnl = effective_position * (pnl_percent / 100)

                    # Комиссии
                    commission_rate = Config.DEFAULT_COMMISSION_RATE
                    entry_commission = effective_position * commission_rate
                    exit_commission = effective_position * commission_rate
                    total_commission = entry_commission + exit_commission

                    # Применяем ограничение isolated margin при принудительном закрытии
                    capped_pnl = self.cap_loss_to_margin(gross_pnl, entry_commission, exit_commission)

                    # Логирование
                    if capped_pnl != gross_pnl - total_commission:
                        print(f"[FORCE CLOSE CAP] Position {pair}: "
                              f"Original PnL: {gross_pnl - total_commission:.2f}, "
                              f"Capped to: {capped_pnl:.2f}")

                    # Обновляем simulation_result
                    result['close_price'] = last_price
                    result['close_time'] = simulation_end_time
                    result['close_reason'] = 'period_end'
                    result['pnl_usd'] = capped_pnl
                    result['pnl_percent'] = pnl_percent
                    result['commission_usd'] = total_commission
                    result['is_closed'] = True

                    # Обновляем позицию
                    position['is_closed'] = True
                    position['close_reason'] = 'period_end'
                    position['close_time'] = simulation_end_time
                    position['close_price'] = last_price
                    position['pnl_usd'] = capped_pnl

                    self._close_position_internal(position)
                else:
                    # Если нет данных, закрываем с PnL=0 (комиссии съедают)
                    position['is_closed'] = True
                    position['close_reason'] = 'period_end'
                    position['close_time'] = simulation_end_time
                    position['pnl_usd'] = 0

                    self._close_position_internal(position)

        # Очищаем открытые позиции
        self.open_positions.clear()

    def get_summary(self):
        """
        Возвращает итоговую сводку симуляции

        Returns:
            dict: Словарь с метриками
        """
        final_equity = self.available_capital + self.total_pnl
        max_drawdown_usd = self.initial_capital - self.min_equity
        max_drawdown_percent = (max_drawdown_usd / self.initial_capital) * 100 if self.initial_capital > 0 else 0

        # Подсчет Win Rate
        wins = sum(1 for trade in self.closed_trades if trade['pnl_usd'] > 0)
        total_trades = len(self.closed_trades)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

        return {
            'initial_capital': self.initial_capital,
            'final_equity': final_equity,
            'total_pnl': self.total_pnl,
            'total_pnl_percent': (self.total_pnl / self.initial_capital * 100) if self.initial_capital > 0 else 0,
            'max_concurrent_positions': self.max_concurrent_positions,
            'min_equity': self.min_equity,
            'max_drawdown_usd': max_drawdown_usd,
            'max_drawdown_percent': max_drawdown_percent,
            'total_commission_paid': self.total_commission_paid,
            'total_trades': total_trades,
            'wins': wins,
            'losses': total_trades - wins,
            'win_rate': win_rate,
            'stats': self.stats,
            'closed_trades': self.closed_trades,
        }
