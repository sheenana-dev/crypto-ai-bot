GRID_PARAMS = {
    "BTC/USDT:USDT": {
        "num_grids": 10,
        "grid_spacing_pct": 0.002,   # 0.2% tightened from 0.3% — more fills, less profit per trade
        "order_size_usdt": 25,       # Best performer
        "range_pct": 0.02,           # 2% total range
    },
    "ETH/USDT:USDT": {
        "num_grids": 10,
        "grid_spacing_pct": 0.003,   # 0.3% tightened from 0.5% — need more fills
        "order_size_usdt": 20,
        "range_pct": 0.03,
    },
    "SOL/USDT:USDT": {
        "num_grids": 10,
        "grid_spacing_pct": 0.003,   # 0.3% tightened from 0.5% — more volatile, should fill more
        "order_size_usdt": 20,
        "range_pct": 0.03,
    },
    "XRP/USDT:USDT": {
        "num_grids": 10,
        "grid_spacing_pct": 0.004,   # 0.4% tightened from 0.7% — balance fills vs profit
        "order_size_usdt": 25,
        "range_pct": 0.04,
    },
    "DOGE/USDT:USDT": {
        "num_grids": 10,
        "grid_spacing_pct": 0.008,   # 0.8% — widened from 0.5%, 10% WR was too tight
        "order_size_usdt": 15,       # Reduced from $20 — weakest performer (10% WR), limit exposure
        "range_pct": 0.08,
    },
}

# --- DCA Config ---
DCA_PARAMS = {
    "entry_pct": 0.05,         # Buy 5% of DCA reserve per entry
    "additional_drop_pct": 0.03,  # Buy more if price drops another 3%
    "max_entries_per_dip": 3,
    "take_profit_pct": 0.01,   # Take profit at avg entry + 1% — 2% was too greedy, missed exits
}
