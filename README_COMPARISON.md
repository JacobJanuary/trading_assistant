# Signal Processing Comparison - Complete Documentation

This directory contains a comprehensive analysis comparing the signal processing logic between the **REFERENCE implementation** (correct) and the **CURRENT implementation** (with bugs).

## Document Index

### 1. **QUICK_REFERENCE.txt** (START HERE!)
   - Quick lookup for all 6 bugs
   - Current vs should-be code snippets
   - Priority fix order
   - ~150 lines, easy scan

### 2. **COMPARISON_SUMMARY.md** (Executive Summary)
   - High-level overview of findings
   - What each bug means in plain language
   - Key differences in a table format
   - Architecture comparison (flowcharts)
   - Action items organized by priority
   - ~300 lines

### 3. **IMPLEMENTATION_COMPARISON.md** (Detailed Analysis)
   - In-depth comparison of all 8 key areas:
     1. Signal filtering by score
     2. Position management (open/close)
     3. Trailing stop calculation
     4. Stop loss & liquidation
     5. PnL calculation with isolated margin
     6. Simulation_end_time handling
     7. Commission calculations
     8. Capital management
   - Line-by-line code comparison
   - ~400 lines

### 4. **BUGS_AND_FIXES.md** (Implementation Guide)
   - All 6 bugs with detailed fixes
   - Complete code examples (WRONG vs CORRECT)
   - Impact analysis for each bug
   - Testing checklist
   - ~300 lines

---

## The 6 Critical Bugs

| # | Bug | Severity | Impact |
|---|-----|----------|--------|
| 1 | Trailing Stop Ratchets UP | CRITICAL | Wrong exit prices |
| 2 | Floating PnL Always Zero | CRITICAL | Wrong equity metrics |
| 3 | No Loss Capping (TP/SL) | CRITICAL | Losses exceed margin |
| 4 | No Slippage on Stop Loss | HIGH | Too optimistic P&L |
| 5 | No Loss Capping (Period End) | CRITICAL | Negative equity possible |
| 6 | No Signal Filtering Check | MEDIUM | May allow invalid signals |

---

## Quick Navigation

### If you want to...

**Understand the bugs quickly**
→ Read `QUICK_REFERENCE.txt` (5 mins)

**Get an executive overview**
→ Read `COMPARISON_SUMMARY.md` (10 mins)

**See detailed analysis**
→ Read `IMPLEMENTATION_COMPARISON.md` (20 mins)

**Implement the fixes**
→ Read `BUGS_AND_FIXES.md` (30 mins)

**Reference the correct code**
→ See `/home/elcrypto/calk_wk/backtest_both.py`

---

## Key Findings

### Most Critical Issue
**Bug #2: Floating PnL always returns 0** because `market_data_by_pair=None` is passed.
- This affects ALL equity calculations
- Min equity is understated
- Max drawdown is understated
- Back-test results are too optimistic

### Most Dangerous Issue
**Bug #3 & #5: No loss capping** allows losses to exceed account margin.
- Isolated margin rules violated
- Account can go negative
- Doesn't match real trading behavior

### Most Subtle Issue
**Bug #1: Trailing stop ratchets UP instead of DOWN** for LONG positions.
- Violates trailing stop semantics
- Positions exit at worse prices
- Users don't expect this behavior

---

## Implementation Status

### REFERENCE Implementation (backtest_both.py)
- ✅ Explicit signal filtering (2 conditions)
- ✅ Proper floating PnL calculation
- ✅ Correct trailing stop logic
- ✅ Loss capping with cap_loss_to_margin()
- ✅ Slippage on stop loss (0.05%)
- ✅ Period-end closure with capping
- ✅ Correct equity metrics

### CURRENT Implementation
- ❌ No explicit signal filtering validation
- ❌ Floating PnL always 0 (bug)
- ❌ Trailing stop can move UP (bug)
- ❌ No loss capping anywhere (bug)
- ❌ No slippage on stop loss (missing)
- ❌ Period-end closure not capped (bug)
- ❌ Equity metrics wrong (from bug #2)

---

## Files to Modify

```
/home/elcrypto/trading_assistant/
├── database.py
│   ├── Line 1575-1577: Trailing stop ratcheting (BUG #1)
│   ├── Line 2947: Floating PnL calculation (BUG #2)
│   ├── Line 1528-1531: Slippage on stop loss (BUG #4)
│   └── Line 3259-3309: Signal filtering (BUG #6)
│
└── trading_simulation.py
    ├── Line 282-291: Loss capping in _simulate_fixed_tp_sl (BUG #3)
    ├── Line 252-254, 262-264: Slippage on TP/SL (BUG #4)
    └── Line 451: Loss capping in force_close_all_positions (BUG #5)
```

---

## Recommended Fix Order

1. **BUG #2** (Floating PnL) - 10 lines
   - Affects all equity calculations first
   - Should be fixed first to get accurate metrics

2. **BUG #3** (Loss capping in _simulate_fixed_tp_sl) - 5 lines
   - Prevents margin violations
   - Required for correct PnL

3. **BUG #5** (Loss capping in force_close_all_positions) - 20 lines
   - Prevents period-end margin violations
   - Complex but critical

4. **BUG #1** (Trailing stop ratcheting) - 2 lines
   - Simple fix
   - Affects trailing stop behavior

5. **BUG #4** (Slippage) - 5 lines
   - Adds realism to simulation
   - Nice-to-have after core fixes

6. **BUG #6** (Signal filtering) - 5 lines
   - Adds validation
   - Good practice but lower priority

---

## Verification Checklist

After applying all fixes, verify:

```
[ ] Trailing stop only moves DOWN for LONG positions
[ ] Trailing stop only moves UP for SHORT positions
[ ] Floating PnL uses actual market data (not None)
[ ] All losses capped to (margin - entry_commission)
[ ] Period-end closures cap loss to 95% of margin
[ ] Stop loss executions have 0.05% slippage
[ ] No negative equity values in results
[ ] Min equity matches reference implementation
[ ] Equity curve shape similar to reference
[ ] Win/loss ratios similar to reference
[ ] Max drawdown calculation includes floating losses
[ ] Back-test results reproducible
```

---

## Testing Strategy

### Unit Testing
- Test each fix independently
- Verify isolated functionality

### Integration Testing
- Run full backtest with fixed code
- Compare results against reference

### Regression Testing
- Ensure no new bugs introduced
- Check edge cases (liquidations, period-end)

### Validation Testing
- Compare equity curves
- Compare P&L distributions
- Compare win/loss counts

---

## Reference Code

The correct implementation is in:
```
/home/elcrypto/calk_wk/backtest_both.py
```

Key functions to reference:
- `cap_loss_to_margin()` - Lines 106-136
- `simulate_position_lifecycle()` - Lines 307-449
- `calculate_floating_pnl()` - Lines 451-488
- `process_combination_v2()` - Lines 527-803 (main logic)

---

## Questions?

When confused about expected behavior, refer to `backtest_both.py` which is the source of truth.

The REFERENCE implementation:
- Uses explicit filtering for both score_week AND score_month
- Calculates floating PnL with actual market data
- Caps all losses to margin minus entry commission
- Applies slippage on all stop loss executions
- Handles period-end closure with forced liquidation checks

---

## Summary

This codebase has a working reference implementation that should be studied as the gold standard. The current implementation has taken shortcuts (or bugs) that make results incorrect. The fixes are straightforward but critical for correct back-testing.

Start with `QUICK_REFERENCE.txt` and work through the documents in order for a complete understanding.

