GRID_PARAMS = {
    "BTC/USDT:USDT": {
        "num_grids": 10,
        "grid_spacing_pct": 0.002,   # 0.2% between grid levels
        "order_size_usdt": 22,       # USDT per grid order (22 * 5x lev = $110 notional > $100 min)
        "range_pct": 0.04,           # 4% total range (2% above + 2% below)
    },
    "ETH/USDT:USDT": {
        "num_grids": 10,
        "grid_spacing_pct": 0.006,
        "order_size_usdt": 22,
        "range_pct": 0.05,
    },
}

# --- DCA Config ---
DCA_PARAMS = {
    "entry_pct": 0.05,         # Buy 5% of DCA reserve per entry
    "additional_drop_pct": 0.03,  # Buy more if price drops another 3%
    "max_entries_per_dip": 3,
    "take_profit_pct": 0.04,   # Take profit at avg entry + 4%
}
