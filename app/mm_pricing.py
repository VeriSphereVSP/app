from math import log10
from typing import Dict
from .oracle import get_gold_price_usd_per_oz, get_avax_price_usd

def compute_mm_prices(
    net_vsp: int,
    unit_au: float,        # kept for future extensibility, unused now
    spread_rate: float,
) -> Dict[str, float]:
    """
    Canonical VSP pricing:

    T = net VSP sold by MM (>= 0)
    base = log10(T + 10) ** 2
    market_usd = base * gold_price_usd

    market_usd is NEVER zero.
    """

    T = max(0, int(net_vsp))

    gold = get_gold_price_usd_per_oz()
    avax = get_avax_price_usd()

    base = log10(T + 10) ** 2
    market_usd = base * unit_au * gold

    # Defensive: absolute floor
    if market_usd <= 0:
        market_usd = gold * 0.0001
    """
    print(
        "[MM_DEBUG]",
        f"net_vsp={net_vsp}",
        f"T={T}",
        f"unit_au={unit_au}",
        f"gold={gold}",
        f"price={market_usd}",
        flush=True,
    )
    """

    buy_usd = market_usd * spread_rate
    sell_usd = market_usd / spread_rate

    return {
        "market_usd": market_usd,
        "buy_usd": buy_usd,
        "sell_usd": sell_usd,
        "buy_avax": buy_usd / avax,
        "sell_avax": sell_usd / avax,
        "gold_usd_per_oz": gold,
        "avax_usd": avax,
    }

