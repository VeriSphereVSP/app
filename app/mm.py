import math
from typing import Dict


def compute_mm_prices(
    net_vsp: int,
    unit_au: float,
    spread_rate: float,
    gold_price_usd: float,
) -> Dict[str, float]:
    """
    market_usdc = (log10(net_vsp)^2) * unit_au * gold_price_usd
    buy_usdc    = market_usdc / spread_rate
    sell_usdc   = market_usdc * spread_rate
    """

    N = max(net_vsp, 1)

    market_usdc = (math.log10(N) ** 2) * unit_au * gold_price_usd

    return {
        "market_usdc": market_usdc,
        "buy_usdc": market_usdc / spread_rate,
        "sell_usdc": market_usdc * spread_rate,
    }

