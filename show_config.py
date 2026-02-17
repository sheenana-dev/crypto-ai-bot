#!/usr/bin/env python3
"""Display current bot configuration."""

from config import settings, grid_config

print('=== TRADING SETUP ===\n')
mode = 'TESTNET' if settings.TESTNET else '🔴 LIVE TRADING'
print(f'Mode: {mode}')
print(f'Pairs: {", ".join(settings.PAIRS)}')
print(f'Leverage: {settings.LEVERAGE}x')
print(f'Bot Cycle: Every 1 minute (dynamic-style)')
print(f'Analysis Timeframe: 15m')

print('\n=== CAPITAL ===\n')
print(f'Total Capital: ${settings.TOTAL_CAPITAL}')
print(f'Grid Capital: ${settings.GRID_CAPITAL} ({settings.GRID_CAPITAL/settings.TOTAL_CAPITAL*100:.0f}%)')
print(f'DCA Reserve: ${settings.DCA_RESERVE} ({settings.DCA_RESERVE/settings.TOTAL_CAPITAL*100:.0f}%)')
print(f'Emergency Buffer: ${settings.EMERGENCY_BUFFER} ({settings.EMERGENCY_BUFFER/settings.TOTAL_CAPITAL*100:.0f}%)')

print('\n=== GRID CONFIG (BASE SPACING) ===\n')
for pair, config in grid_config.GRID_PARAMS.items():
    if pair in settings.PAIRS:
        print(f'{pair}:')
        print(f'  Base Spacing: {config["grid_spacing_pct"]*100:.1f}%')
        print(f'  Order Size: ${config["order_size_usdt"]}')
        print(f'  Num Grids: {config["num_grids"]}')

print('\n=== ADAPTIVE SPACING (RANGING) ===')
print('Multiplier: 1.5x when ADX < 23, 1.0x when ADX 23-25\n')
for pair, config in grid_config.GRID_PARAMS.items():
    if pair in settings.PAIRS:
        base = config['grid_spacing_pct'] * 100
        adaptive = base * 1.5
        print(f'{pair}: {base:.1f}% → {adaptive:.1f}%')

print('\n=== REGIME DETECTION ===\n')
print(f'ADX Threshold: {settings.ADX_TRENDING_THRESHOLD} (above = TRENDING)')
print(f'ADX Period: {settings.ADX_PERIOD}')
print('Behavior: Pause grid when TRENDING, trade when RANGING')

print('\n=== RISK LIMITS ===\n')
print(f'Stop Loss: REMOVED (kill switch + daily limit protect portfolio)')
print(f'Daily Loss Limit: {settings.DAILY_LOSS_LIMIT_PCT*100:.0f}% (${settings.TOTAL_CAPITAL * settings.DAILY_LOSS_LIMIT_PCT:.0f})')
print(f'Kill Switch: {settings.KILL_SWITCH_DRAWDOWN*100:.0f}% drawdown (${settings.TOTAL_CAPITAL * settings.KILL_SWITCH_DRAWDOWN:.0f})')
print(f'Max Open Orders: {settings.MAX_OPEN_ORDERS}')

print('\n=== DCA CONFIG ===\n')
dca = grid_config.DCA_PARAMS
print(f'Entry: {dca["entry_pct"]*100:.0f}% of DCA reserve')
print(f'Drop Interval: {dca["additional_drop_pct"]*100:.0f}% (buy more if drops)')
print(f'Max Entries: {dca["max_entries_per_dip"]}')
print(f'Take Profit: {dca["take_profit_pct"]*100:.0f}% above avg entry')

print('\n=== TECHNICAL INDICATORS ===\n')
print(f'RSI Period: {settings.RSI_PERIOD}')
print(f'EMA Short: {settings.EMA_SHORT}')
print(f'EMA Long: {settings.EMA_LONG}')
print(f'Bollinger Bands: {settings.BB_PERIOD} period, {settings.BB_STD} std dev')
