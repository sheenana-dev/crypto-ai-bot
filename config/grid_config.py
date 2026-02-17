GRID_PARAMS = {
    "BTC/USDT:USDT": {
        "num_grids": 6,               # 3 buy + 3 sell — concentrated near price (dynamic-style)
        "grid_spacing_pct": 0.008,     # 0.8% base — $5.90/RT with adaptive ×1.5
        "order_size_usdt": 25,
        "range_pct": 0.048,            # 4.8% total range (6 grids × 0.8%)
    },
    "ETH/USDT:USDT": {
        "num_grids": 6,
        "grid_spacing_pct": 0.008,     # 0.8% base — 4.9% daily range supports wider spacing
        "order_size_usdt": 20,
        "range_pct": 0.048,            # 4.8% total range
    },
    "SOL/USDT:USDT": {
        "num_grids": 6,
        "grid_spacing_pct": 0.010,     # 1.0% base — more volatile (4%+ daily), needs wider
        "order_size_usdt": 20,
        "range_pct": 0.06,             # 6% total range (6 grids × 1.0%)
    },
    "XRP/USDT:USDT": {
        "num_grids": 6,
        "grid_spacing_pct": 0.010,     # 1.0% base — most volatile (6.6% daily), widest spacing
        "order_size_usdt": 25,
        "range_pct": 0.06,             # 6% total range
    },
    "DOGE/USDT:USDT": {
        "num_grids": 10,
        "grid_spacing_pct": 0.012,   # 1.2% — Phase 1: widened from 0.8% (weakest performer, needs widest spacing)
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
