import ccxt
import os
from dotenv import load_dotenv

load_dotenv()
exchange = ccxt.binanceusdm({
    'apiKey': os.getenv('BINANCE_API_KEY'),
    'secret': os.getenv('BINANCE_API_SECRET'),
})
exchange.set_sandbox_mode(False)

# Check balance
balance = exchange.fetch_balance()
usdt = balance.get('USDT', {})
print("\n=== ACCOUNT BALANCE ===")
print(f"Free: ${usdt.get('free', 0):.2f}")
print(f"Used: ${usdt.get('used', 0):.2f}")
print(f"Total: ${usdt.get('total', 0):.2f}")
if 'info' in balance:
    print(f"Wallet: ${float(balance['info'].get('totalWalletBalance', 0)):.2f}")
    print(f"Unrealized P&L: ${float(balance['info'].get('totalUnrealizedProfit', 0)):.2f}")

# Check positions
print("\n=== OPEN POSITIONS ===")
positions = exchange.fetch_positions()
has_positions = False
for pos in positions:
    amt = float(pos.get('contracts', 0) or 0)
    if amt > 0:
        has_positions = True
        print(f"\n{pos['symbol']}: {pos['side'].upper()} {amt}")
        print(f"  Entry: ${float(pos['entryPrice']):.2f}")
        print(f"  Mark: ${float(pos['markPrice']):.2f}")
        print(f"  Unrealized P&L: ${float(pos['unrealizedPnl']):.2f}")
        print(f"  Notional: ${amt * float(pos['entryPrice']):.2f}")

if not has_positions:
    print("No open positions")
