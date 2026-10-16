"""30-day backtest of baseline vs optimal on NYISO N.Y.C. prices. Usage: uv run scripts/backtest.py [days] [rt|da]"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import time

from grid.optimize import backtest

if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    mode = sys.argv[2] if len(sys.argv) > 2 else "rt"
    t = time.perf_counter()
    r = backtest(days, mode)
    for d in r["days"]:
        print(f"{d['date']}  baseline ${d['baseline_usd']:>9,.2f}  optimal ${d['optimal_usd']:>9,.2f}"
              f"  saved ${d['saved_usd']:>8,.2f}  peak {d['baseline_peak_kw']:,.0f}->{d['optimal_peak_kw']:,.0f} kW"
              f"  viol {d['violations']}")
    tt = r["totals"]
    print(f"mode={mode} nights={tt['nights']} baseline ${tt['baseline_usd']:,.2f} optimal ${tt['optimal_usd']:,.2f}"
          f" saved ${tt['saved_usd']:,.2f} ({tt['pct']}%) in {time.perf_counter() - t:.1f}s")
