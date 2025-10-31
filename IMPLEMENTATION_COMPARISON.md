# DETAILED COMPARISON: Signal Processing Logic
## Reference vs Current Implementation

### FILES COMPARED
- REFERENCE: `/home/elcrypto/calk_wk/backtest_both.py` (correct implementation)
- CURRENT: `/home/elcrypto/trading_assistant/database.py` (process_scoring_signals_batch_v2) + `/home/elcrypto/trading_assistant/trading_simulation.py`

---

## 1. SIGNAL FILTERING BY SCORE_WEEK AND SCORE_MONTH

### REFERENCE (backtest_both.py, lines 540-544):
```python
filtered_signals = [
    s for s in all_signals_cached
    if s['score_week'] >= score_w and s['score_month'] >= score_m
]
```
**Characteristics:**
- Simple, direct filtering BEFORE any processing
- Filters applied during initialization phase
- All signals loaded at once globally (shared memory)
- Minimum thresholds: score_week >= score_w AND score_month >= score_m

### CURRENT (database.py, lines 3259, 3309):
```python
# Line 3259: All signals loaded first
all_signals = get_scoring_signals(db, date_filter, score_week_min, score_month_min, allowed_hours)

# Line 3309: Later sorted by score_week (DESCENDING)
sorted_signals = sorted(signals, key=lambda x: x.get('score_week', 0), reverse=True)
```
**Issues:**
1. **NO EXPLICIT FILTERING** - Relies on get_scoring_signals() which takes score_week_min, score_month_min
2. **MISSING VALIDATION** - No explicit check that score_month >= score_month_min in process_scoring_signals_batch_v2
3. **ASSUMPTION PROBLEM** - Assumes filtering happens in get_scoring_signals, not in main function
4. **SORTING ORDER** - Sorts by score_week DESC, which is correct, BUT doesn't enforce score_month minimum

**BUG**: Signals with high score_week but LOW score_month could pass through (if get_scoring_signals has bug)

---

## 2. POSITION MANAGEMENT (OPEN/CLOSE LOGIC)

### REFERENCE (backtest_both.py, lines 568-655):

**Position Tracking:**
```python
open_positions = {}  # dict: pair -> outcome

# Loop through sorted waves
for wave_time in sorted(signals_by_wave.keys()):
    # STEP 1: Close positions due to close
    positions_to_close = []
    for pair, trade_outcome in open_positions.items():
        if trade_outcome['close_time'] <= wave_time:
            positions_to_close.append(pair)
    
    # Process closures
    for pair in positions_to_close:
        trade_outcome = open_positions[pair]
        # ... PnL calculation ...
        available_capital += position_size + net_pnl
        del open_positions[pair]
    
    # STEP 2: Open new positions
    for signal_candidate in wave_candidates:
        if trades_taken_this_wave >= max_trades:
            break
        if available_capital < position_size:
            break
        
        pair = signal_candidate['pair_symbol']
        
        # Check for existing position
        pair_has_position = False
        for open_pair, pos_info in open_positions.items():
            if open_pair == pair:
                if pos_info['close_time'] > wave_time:
                    pair_has_position = True
                    break
        
        if pair_has_position:
            continue
        
        available_capital -= position_size
        # ... simulate position ...
        open_positions[pair] = outcome
```

**Key Characteristics:**
- Sequential wave processing (sorted by timestamp)
- Position closure BEFORE opening new trades
- Check for duplicate pairs (pair_has_position)
- Capital management: deduct BEFORE opening, return on close
- Floating PnL calculated AFTER closing due positions (line 608)

### CURRENT (database.py + trading_simulation.py):

**TradingSimulation.open_position() - lines 90-161:**
```python
def open_position(self, signal, entry_price, market_data, simulation_end_time=None):
    pair_symbol = signal['pair_symbol']
    
    # Check can open
    can_open, reason = self.can_open_position(pair_symbol)
    if not can_open:
        self.stats['skipped_' + reason] += 1
        return {'success': False, 'reason': reason}
    
    # Deduct capital
    self.available_capital -= self.position_size
    
    # Simulate (calls calculate_trailing_stop_exit or _simulate_fixed_tp_sl)
    result = ...
    
    # Add to open_positions
    if not position_info['is_closed']:
        self.open_positions[pair_symbol] = position_info
    else:
        self._close_position_internal(position_info)
    
    self.stats['trades_opened'] += 1
    self.max_concurrent_positions = max(..., len(self.open_positions))
    return {'success': True, 'position': position_info}
```

**can_open_position() - lines 73-88:**
```python
def can_open_position(self, pair_symbol):
    if self.available_capital < self.position_size:
        return False, 'insufficient_capital'
    
    if pair_symbol in self.open_positions:
        return False, 'duplicate_pair'
    
    return True, 'ok'
```

**Issues in CURRENT:**
1. **DOUBLE POSITION CHECK BUG** - `can_open_position` only checks current open_positions
   - But position might have close_time AFTER current wave time
   - Reference implementation checks: `if pos_info['close_time'] > wave_time`
   - Current has NO such check!

2. **FLOATING PnL CALCULATION ISSUE** - Line 2947 in database.py:
```python
sim.update_equity_metrics(wave_time, market_data_by_pair=None)
```
   - Passes `market_data_by_pair=None` (!) 
   - So floating PnL is NOT actually calculated
   - update_equity_metrics() needs market_data but gets None

3. **POSITION CLOSING LOGIC** - process_scoring_signals_batch_v2, lines 2941-2943:
```python
closed_pairs = sim.close_due_positions(wave_time)
```
   - close_due_positions() checks: `if position['close_time'] <= wave_time`
   - This is CORRECT, but then positions are immediately deleted
   - Missing: verification that position['close_time'] is actually set properly

---

## 3. TRAILING STOP CALCULATION

### REFERENCE (backtest_both.py, lines 336-411):

**Initialization:**
```python
# Line 336-337: Calculate activation and SL prices
sl_price = entry_price * (1 - stop_loss_percent / 100) if is_long else entry_price * (1 + stop_loss_percent / 100)
ts_activation_price = entry_price * (1 + trailing_activation_pct / 100) if is_long else entry_price * (1 - trailing_activation_pct / 100)

# Line 339-341: Initialize tracking variables
is_ts_active = False
trailing_stop_price = None
best_price = entry_price
```

**Activation Logic (lines 392-401 for LONG):**
```python
if is_long:
    best_price = max(best_price, candle_high)
    if not is_ts_active and best_price >= ts_activation_price:
        is_ts_active = True
    if is_ts_active:
        new_ts_price = best_price * (1 - trailing_distance_pct / 100)
        trailing_stop_price = max(trailing_stop_price, new_ts_price) if trailing_stop_price else new_ts_price
        if candle_low <= trailing_stop_price:
            outcome = {..., "close_reason": "trailing_stop"}
            break
```

**Key Points:**
- Trailing price is STRICTLY DESCENDING for Long (max() used)
- Once activated, trailing_stop_price only moves DOWN (uses max())
- Activation price is checked BEFORE update
- Best price updated after activation check

### CURRENT (database.py, calculate_trailing_stop_exit, lines 1561-1584 for LONG):

```python
if is_short:
    # SHORT logic (lines 1536-1559)
    if low_price < best_price_for_trailing:
        best_price_for_trailing = low_price
    
    if not is_trailing_active and best_price_for_trailing <= activation_price:
        is_trailing_active = True
        trailing_stop_price = best_price_for_trailing * (1 + trailing_distance_pct / 100)
    
    if is_trailing_active:
        new_stop = best_price_for_trailing * (1 + trailing_distance_pct / 100)
        if new_stop < trailing_stop_price:
            trailing_stop_price = new_stop
    
    if is_trailing_active and candle_time != activation_candle_time and high_price >= trailing_stop_price:
        # Close position
else:  # LONG (lines 1561-1584)
    if high_price > best_price_for_trailing:
        best_price_for_trailing = high_price
    
    if not is_trailing_active and best_price_for_trailing >= activation_price:
        is_trailing_active = True
        trailing_stop_price = best_price_for_trailing * (1 - trailing_distance_pct / 100)
    
    if is_trailing_active:
        new_stop = best_price_for_trailing * (1 - trailing_distance_pct / 100)
        if new_stop > trailing_stop_price:
            trailing_stop_price = new_stop
    
    if is_trailing_active and candle_time != activation_candle_time and low_price <= trailing_stop_price:
        # Close position
```

**Critical Difference - REFERENCE vs CURRENT:**

| Aspect | REFERENCE | CURRENT | Issue |
|--------|-----------|---------|-------|
| Initial best_price | entry_price | entry_price | OK |
| TS activation check | Before price update | Before price update | OK |
| TS update logic for LONG | `trailing_stop_price = max(trailing_stop_price, new_ts_price)` | `if new_stop > trailing_stop_price: trailing_stop_price = new_stop` | **DIFFERENT** |
| TS ratcheting for LONG | Strictly descending | Can go UP in CURRENT | **BUG in CURRENT** |
| Allow activation on same candle | NO (implicit) | NO (explicit: `candle_time != activation_candle_time`) | CURRENT better |

**CRITICAL BUG in CURRENT:**
```python
# WRONG - trailing stop can increase (ratchet UP)
if new_stop > trailing_stop_price:
    trailing_stop_price = new_stop
```
Should be (for LONG):
```python
# CORRECT - trailing stop only decreases (ratchets DOWN)
if new_stop < trailing_stop_price:
    trailing_stop_price = new_stop
```

This means CURRENT implementation allows trailing_stop_price to go UP for Long positions, which violates trailing stop semantics!

---

## 4. STOP LOSS AND LIQUIDATION HANDLING

### REFERENCE (backtest_both.py, lines 356-378, 380-390):

**Liquidation (lines 356-378):**
```python
liquidation_loss_pct = -(100 / strat['leverage']) * liquidation_threshold
if unrealized_pnl_pct <= liquidation_loss_pct:
    max_loss_percent = (position_margin - entry_commission) / effective_position * 100
    
    if is_long:
        liquidation_price = entry_price * (1 - max_loss_percent / 100)
        actual_liquidation_price = max(liquidation_price, candle_low)
    else:
        liquidation_price = entry_price * (1 + max_loss_percent / 100)
        actual_liquidation_price = min(liquidation_price, candle_high)
    
    outcome = {
        "close_price": actual_liquidation_price,
        "close_reason": "liquidation"
    }
    break
```

**Stop Loss (lines 380-390):**
```python
if (is_long and candle_low <= sl_price) or (not is_long and candle_high >= sl_price):
    slippage = strat.get('slippage_percent', 0) / 100
    if is_long:
        actual_sl_price = sl_price * (1 - slippage)
    else:
        actual_sl_price = sl_price * (1 + slippage)
    
    outcome = {"close_price": actual_sl_price, "close_reason": "stop_loss"}
    break
```

**Key Points:**
- Liquidation checked FIRST (priority)
- Liquidation capped at margin minus entry commission
- Stop Loss has SLIPPAGE applied (0.05%)
- Both checked within Phase 1 (first 24 hours)

### CURRENT (trading_simulation.py, _simulate_fixed_tp_sl, lines 230-265):

```python
# Liquidation
if unrealized_pnl_pct <= liquidation_loss_pct:
    is_closed = True
    close_reason = 'liquidation'
    close_price = low_price if is_long else high_price
    close_time = current_time
    break

# TP/SL (no slippage!)
if is_short:
    if low_price <= tp_price:
        close_reason = 'take_profit'
        close_price = tp_price
    elif high_price >= sl_price:
        close_reason = 'stop_loss'
        close_price = sl_price
else:  # LONG
    if high_price >= tp_price:
        close_reason = 'take_profit'
        close_price = tp_price
    elif low_price <= sl_price:
        close_reason = 'stop_loss'
        close_price = sl_price
```

**Issues in CURRENT:**
1. **NO SLIPPAGE** - Stop loss and TP executed at exact price, not worse price
   - Reference: actual_sl_price = sl_price * (1 - slippage)  (0.05% worse)
   - Current: close_price = sl_price (exact)
   - **Missing realism feature**

2. **LIQUIDATION CAPPING MISSING** - Not applied in _simulate_fixed_tp_sl
   - Reference caps to margin minus entry commission
   - Current uses actual candle low/high
   - **Can result in losses > position_margin**

### CURRENT (calculate_trailing_stop_exit, lines 1509-1533):

```python
# Liquidation check
if unrealized_pnl_pct <= liquidation_loss_pct:
    is_closed = True
    close_reason = 'liquidation'
    close_price = low_price if is_long else high_price
    close_time = candle_time
    continue  # Note: continue, not break!

# Stop Loss (no slippage!)
if (is_long and low_price <= sl_price) or (is_short and high_price >= sl_price):
    is_closed = True
    close_reason = 'stop_loss'
    close_price = sl_price
    close_time = candle_time
    continue
```

**Issues in calculate_trailing_stop_exit:**
1. **NO SLIPPAGE** on stop_loss
2. **LIQUIDATION CAPPING MISSING**
3. Uses `continue` instead of `break` - continues processing after liquidation (BUG)

---

## 5. PnL CALCULATION (ESPECIALLY WITH ISOLATED MARGIN)

### REFERENCE (backtest_both.py, lines 106-136 cap_loss_to_margin, 586-598):

**cap_loss_to_margin function:**
```python
def cap_loss_to_margin(gross_pnl: float, entry_commission: float,
                       exit_commission: float, position_margin: float) -> float:
    max_loss = -(position_margin - entry_commission)
    net_pnl = gross_pnl - entry_commission - exit_commission
    capped_pnl = max(net_pnl, max_loss)
    return capped_pnl
```

**Applied in main loop (lines 586-598):**
```python
effective_position = position_size * strat['leverage']
if is_long:
    pnl_percent = ((close_price - entry_price) / entry_price) * 100
else:
    pnl_percent = ((entry_price - close_price) / entry_price) * 100

gross_pnl = effective_position * (pnl_percent / 100)
exit_commission = effective_position * strat['commission_rate']
net_pnl = gross_pnl - trade_outcome['entry_commission'] - exit_commission

# Ограничиваем убыток размером маржи (изолированная маржа)
net_pnl = cap_loss_to_margin(gross_pnl, trade_outcome['entry_commission'],
                              exit_commission, position_size)
```

**Applied twice more (lines 733-734, 669-687):**
```python
# Second application (line 733-734)
net_pnl = cap_loss_to_margin(gross_pnl, entry_commission, exit_commission, position_size)

# Third application for period-end closes (lines 669-687)
max_loss_percent = (position_size * 0.95) / effective_position * 100
if pnl_percent < -max_loss_percent:
    if is_long:
        last_price = entry_price * (1 - max_loss_percent / 100)
    else:
        last_price = entry_price * (1 + max_loss_percent / 100)
```

### CURRENT (trading_simulation.py, _simulate_fixed_tp_sl, lines 282-291):

```python
# NO cap_loss_to_margin function!
gross_pnl = effective_position * (pnl_percent / 100)
pnl_usd = gross_pnl - total_commission

# NO CAPPING! Can return losses > position_margin
```

**CRITICAL MISSING FUNCTION**: No cap_loss_to_margin() in current implementation!

### CURRENT (database.py, calculate_trailing_stop_exit, lines 1636-1642):

```python
gross_pnl = effective_position * (result['pnl_percent'] / 100)
result['pnl_usd'] = gross_pnl - total_commission  # NET PnL (NO CAPPING!)
result['gross_pnl_usd'] = gross_pnl
result['commission_usd'] = total_commission
```

**NO CAPPING in current implementation**

### HUGE BUG: Forced Period-End Closure (database.py, lines 657-701):

```python
# Reference logic that's MISSING in current:
for pair, position_info in open_positions.items():
    history = market_data_cached.get(position_info['signal_id'])
    if history:
        last_price = None
        for candle in history:
            if candle['timestamp'] <= simulation_end_time:
                last_price = float(candle['close_price'])
            else:
                break
        
        if last_price:
            entry_price = float(history[0]['open_price'])
            is_long = position_info['signal_action'].upper() in ['BUY', 'LONG']
            effective_position = position_size * strat['leverage']
            
            if is_long:
                pnl_percent = ((last_price - entry_price) / entry_price) * 100
            else:
                pnl_percent = ((entry_price - last_price) / entry_price) * 100
            
            # **CRITICAL**: Cap to 95% of margin
            max_loss_percent = (position_size * 0.95) / effective_position * 100
            
            if pnl_percent < -max_loss_percent:
                if is_long:
                    last_price = entry_price * (1 - max_loss_percent / 100)
                else:
                    last_price = entry_price * (1 + max_loss_percent / 100)
                position_info['close_reason'] = 'forced_liquidation'
            else:
                position_info['close_reason'] = 'forced_period_end'
```

**Current has:**
```python
# force_close_all_positions() in trading_simulation.py, lines 410-481
# NO CAPPING! Just closes at last_price without checking margin limits
```

---

## 6. SIMULATION_END_TIME HANDLING

### REFERENCE (backtest_both.py, lines 831):

```python
GLOBAL_SIMULATION_END_TIME = end_date
```

Passed to simulate_position_lifecycle (line 309):
```python
def simulate_position_lifecycle(signal: Dict, history: List[Dict], stop_loss_percent: float,
                               trailing_activation_pct: float, trailing_distance_pct: float,
                               simulation_end_time: datetime) -> Optional[Dict]:
```

Used in multiple places:
- Line 324-325: Skip candles after simulation_end_time
- Line 439-441: Check if candle_time >= simulation_end_time and close at period_end

### CURRENT (database.py, lines 2862-2874):

```python
if signals:
    last_signal_time = max(s['timestamp'] for s in signals)
    simulation_end_time = last_signal_time + timedelta(hours=48)
else:
    simulation_end_time = None
```

**CRITICAL DIFFERENCE:**
- Reference: Uses end_date from buffer (40 hours before NOW)
- Current: Uses last_signal_time + 48 hours

This means CURRENT backtests different time ranges than REFERENCE!

### Passing to functions:
- Line 3000: `result = sim.open_position(signal, entry_price, market_data, simulation_end_time=simulation_end_time)`
- calculate_trailing_stop_exit() - line 1614-1619: Checks and respects simulation_end_time
- _simulate_fixed_tp_sl() - line 223-228: Also checks and respects simulation_end_time

**Issue**: Both functions have the check, but with different semantics:
- calculate_trailing_stop_exit uses `candle_time >= simulation_end_time`
- _simulate_fixed_tp_sl uses `current_time >= simulation_end_time`

---

## 7. COMMISSION CALCULATIONS

### REFERENCE (backtest_both.py, lines 318-320, 593):

```python
# Initialization
entry_commission = effective_position * strat['commission_rate']
exit_commission = effective_position * strat['commission_rate']
total_commission = entry_commission + exit_commission

# On close (line 593)
exit_commission = effective_position * strat['commission_rate']
net_pnl = gross_pnl - trade_outcome['entry_commission'] - exit_commission

# Also tracked (line 600)
total_commission_paid += (trade_outcome['entry_commission'] + exit_commission)
```

**Structure:**
- Commission_rate: 0.0006 (0.06%)
- Applied once on entry, once on exit
- Total = effective_position * rate * 2

### CURRENT (trading_simulation.py, lines 176-180):

```python
commission_rate = Config.DEFAULT_COMMISSION_RATE
effective_position = self.position_size * self.leverage
entry_commission = effective_position * commission_rate
exit_commission = effective_position * commission_rate
total_commission = entry_commission + exit_commission
```

**Structure:**
- Same logic as reference
- Applied at entry and exit

**ISSUE**: In force_close_all_positions (line 451):
```python
net_pnl = gross_pnl - total_commission
```
Doesn't check if total_commission should be capped by margin!

---

## 8. CAPITAL MANAGEMENT

### REFERENCE (backtest_both.py, lines 553-654):

```python
available_capital = strat['initial_capital']
position_size = strat['position_size_usd']

# Per wave
for wave_time in sorted(signals_by_wave.keys()):
    # Close positions and return capital
    for pair in positions_to_close:
        trade_outcome = open_positions[pair]
        available_capital += position_size + net_pnl
        del open_positions[pair]
    
    # Calculate floating PnL
    floating_pnl = calculate_floating_pnl(open_positions, market_data_cached, wave_time, ...)
    current_equity = available_capital + floating_pnl + len(open_positions) * position_size
    min_equity = min(min_equity, current_equity)
    
    # Open new positions
    for signal_candidate in wave_candidates:
        if available_capital < position_size:
            break
        available_capital -= position_size
```

**Key characteristics:**
- Capital available = actual capital available for new trades
- Deducted BEFORE opening position
- Returned ON CLOSING (position_size + net_pnl)
- Floating PnL calculated from open positions
- min_equity tracks minimum during entire simulation

### CURRENT (database.py + trading_simulation.py):

```python
# In TradingSimulation.__init__
self.available_capital = initial_capital

# In open_position (line 114)
self.available_capital -= self.position_size

# In _close_position_internal (line 327)
self.available_capital += self.position_size

# In update_equity_metrics (line 405)
locked_capital = len(self.open_positions) * self.position_size
current_equity = self.available_capital + floating_pnl + locked_capital
self.min_equity = min(self.min_equity, current_equity)
```

**Same logic as reference, BUT:**

**BUG**: update_equity_metrics() never called with market_data!
- Line 2947: `sim.update_equity_metrics(wave_time, market_data_by_pair=None)`
- Means floating_pnl is always 0
- current_equity only considers available + locked capital
- Does NOT reflect floating losses in open positions

---

## 9. CRITICAL DIFFERENCES SUMMARY

| Aspect | Reference | Current | Impact |
|--------|-----------|---------|--------|
| Signal filtering | Explicit in main loop | Assumed in get_scoring_signals | BUG: May allow invalid signals |
| Position duplicate check | Checks close_time > wave_time | Only checks existence | BUG: May open duplicate |
| Floating PnL calculation | Fully calculated with market data | Called with market_data_by_pair=None | BUG: Always 0, equity wrong |
| Trailing stop ratcheting | max() for DOWN movement | if new_stop > trailing_stop_price | BUG: Can ratchet UP for Long |
| Slippage on SL | Applied (0.05%) | NOT applied | MISSING: Realism |
| Liquidation capping | Explicit cap_loss_to_margin() | No capping function | BUG: Losses can exceed margin |
| Period-end closure | Caps to 95% margin, checks forced_liquidation | No capping in force_close_all_positions | BUG: Losses can exceed margin |
| simulation_end_time | end_date (buffer-adjusted) | last_signal + 48h | DIFFERENT: Different timeframes |
| Commission tracking | Explicit tracking in loop | Tracked but not limited | PARTIAL: Not enforced vs margin |
| Min equity tracking | Calculated with floating PnL | Calculated but floating_pnl=0 | BUG: Wrong min_equity value |

---

## 10. SPECIFIC CODE BUGS TO FIX

### BUG 1: Trailing Stop Ratcheting (calculate_trailing_stop_exit, line 1575-1577)

**CURRENT (WRONG):**
```python
if is_trailing_active:
    new_stop = best_price_for_trailing * (1 - trailing_distance_pct / 100)
    if new_stop > trailing_stop_price:  # <-- WRONG: allows UP movement
        trailing_stop_price = new_stop
```

**SHOULD BE:**
```python
if is_trailing_active:
    new_stop = best_price_for_trailing * (1 - trailing_distance_pct / 100)
    if new_stop < trailing_stop_price:  # Only move DOWN (lower stop for Long)
        trailing_stop_price = new_stop
```

### BUG 2: Floating PnL Not Calculated (database.py, line 2947)

**CURRENT (WRONG):**
```python
sim.update_equity_metrics(wave_time, market_data_by_pair=None)
```

**SHOULD BE:**
```python
# Need to build market_data_by_pair from open positions
market_data_by_pair = {}
for pair, position in sim.open_positions.items():
    signal_id = position['signal_id']
    if signal_id in market_data_cached:
        market_data_by_pair[pair] = market_data_cached[signal_id]

sim.update_equity_metrics(wave_time, market_data_by_pair=market_data_by_pair)
```

### BUG 3: Missing cap_loss_to_margin in _simulate_fixed_tp_sl (trading_simulation.py, line 291)

**CURRENT (WRONG):**
```python
gross_pnl = effective_position * (pnl_percent / 100)
pnl_usd = gross_pnl - total_commission
```

**SHOULD BE:**
```python
gross_pnl = effective_position * (pnl_percent / 100)
entry_commission = effective_position * Config.DEFAULT_COMMISSION_RATE
exit_commission = effective_position * Config.DEFAULT_COMMISSION_RATE

# Apply cap_loss_to_margin (need to implement it)
max_loss = -(self.position_size - entry_commission)
net_pnl = gross_pnl - entry_commission - exit_commission
pnl_usd = max(net_pnl, max_loss)
```

### BUG 4: No Slippage on Stop Loss (multiple places)

**CURRENT (WRONG):**
```python
close_price = sl_price
```

**SHOULD BE:**
```python
slippage = Config.DEFAULT_SLIPPAGE_PERCENT / 100
if is_long:
    close_price = sl_price * (1 - slippage)  # Worse price for long
else:
    close_price = sl_price * (1 + slippage)  # Worse price for short
```

### BUG 5: force_close_all_positions Doesn't Cap Loss (trading_simulation.py, line 451)

**CURRENT (WRONG):**
```python
net_pnl = gross_pnl - total_commission
```

**SHOULD BE:**
```python
max_loss = -(self.position_size - entry_commission)
net_pnl = gross_pnl - total_commission
net_pnl = max(net_pnl, max_loss)  # Cap to margin
```

### BUG 6: close_due_positions Doesn't Handle Edge Case

**Issue**: What if a position's close_time is None? Line 354:
```python
if position['close_time'] and position['close_time'] <= wave_time:
```
Should handle this explicitly.

---

## SUMMARY

The REFERENCE implementation (backtest_both.py) is significantly more robust than CURRENT with:

1. **Explicit loss capping** via cap_loss_to_margin() function
2. **Proper floating PnL calculation** with actual market data
3. **Correct trailing stop semantics** (always descending)
4. **Slippage modeling** on stop loss execution
5. **Proper period-end handling** with forced liquidation protection
6. **Consistent simulation_end_time** usage

CURRENT implementation has 6+ critical bugs that can result in:
- Losses exceeding account margin
- Wrong equity/drawdown calculations
- Incorrect trailing stop behavior
- Unrealistic trade execution

