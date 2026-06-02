#!/usr/bin/env python3
# app/treasury_worker.py
# patch_bundle10_5_part2b_treasury_worker
#
# Bundle 10.5 Part 2 — MM Treasury automation worker.
#
# A standalone cron-loop service (its own container, its own EOA) that keeps the
# MM hot wallet's inventory inside policy bands:
#
#   - SWEEP excess USDC from MM to TREASURY_ADDRESS (custody discipline).
#   - MINT VSP into MM when MM's VSP inventory is low (liquidity for user buys).
#   - BURN VSP from MM when MM's VSP inventory is high (supply discipline; raises
#     the floor, no theft potential, so no rate limit).
#
# Locked design (see ~/verisphere/bundle10_5-part2-design.md and the session
# decisions that refined it):
#
#   * Cap lives ON-CHAIN (VSPToken.maxAllowedSupply, time-based growth cap from
#     Part 2a). The worker READS it; it never recomputes the 5^t curve in Python.
#     Cap is cached with an hourly TTL (slow-moving); totalSupply is read fresh
#     each cycle (can jump on any mint/burn).
#
#   * Self-scaling VSP band, computed each cycle from the cap:
#         target = max(1000, min(0.1 * cap_vsp, 100000))   VSP
#         band_min    = 0.5 * target
#         band_max    = 2.0 * target
#         burn_target = target            (same target both directions)
#     The max(1000, ...) floor means MM aspires to >= 1000 VSP even while the
#     cap is tight early in the curve; the contract + worker shaving handle the
#     gap (mints get shaved to headroom and alerted).
#
#   * Cap-aware mint shaving (wei precision, lesson #14 — no float arithmetic on
#     on-chain amounts):
#         desired   = target_wei - mm_vsp_wei            (when mm_vsp < band_min)
#         headroom  = cap_wei - total_supply_wei
#         amount    = min(desired, headroom)
#         mint only if amount > MINT_DUST_WEI
#     The contract STILL enforces the cap (defense in depth); shaving just avoids
#     a guaranteed revert and logs WARN when it shaves.
#
#   * USDC sweep target derived from the VSP liquidity need via floor price,
#     capped by an absolute custody ceiling:
#         usdc_target = min(target_vsp * floor_price, MM_USDC_TARGET_ABS_MAX)
#         hot_max     = 2.0 * usdc_target          (sweep when above)
#         sweep amount= min(mm_usdc - usdc_target, MM_USDC_SWEEP_CAP_PER_CALL)
#     Floor price read from /api/mm/floor, cached hourly. No USDC "refill": USDC
#     cannot be minted; sweep is one-sided by nature.
#
#   * Master + per-action switches, READ EACH LOOP ITERATION (not cached across
#     iterations). Note: docker-compose env_file is consumed at container start,
#     so changing a switch in env still requires regenerating the resolved env
#     and `docker compose up -d --force-recreate vsp-treasury-worker` to take
#     effect — the read-each-iteration behavior is about not caching within one
#     process lifetime.
#
#   * Sweep destination guard: TREASURY_ADDRESS is snapshotted at startup; if it
#     changes mid-run the worker alerts and exits(1) (container restarts).
#
#   * Alerting: structured records appended to a log file (default
#     /var/log/vsp-treasury-worker.log) AND emitted on stdout. Bundle 8 will
#     tail and forward to Telegram/email later.
#
#   * --dry-run: read + compute + log intended actions, sign nothing.
#   * --once:    run one iteration and exit (cron-style invocation / smoke test).

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

# Ensure app dir on path (mirrors worker.py).
sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("treasury_worker")

from web3 import Web3  # noqa: E402

from config import (  # noqa: E402
    VSP_TOKEN_ADDRESS,
    USDC_ADDRESS,
    MM_ADDRESS,
    TREASURY_ADDRESS,
    APP_API_BASE,  # added by the Part 2b config edit (default http://localhost:8070)
)

# ─────────────────────── env helpers (read each loop) ───────────────────────

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v)
    except ValueError:
        logger.warning("env %s=%r not a float; using default %s", name, v, default)
        return default


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except ValueError:
        logger.warning("env %s=%r not an int; using default %s", name, v, default)
        return default


# ─────────────────────── alerting ───────────────────────

ALERT_LOG_PATH = os.getenv("MM_TREASURY_ALERT_LOG", "/var/log/vsp-treasury-worker.log")


def alert(kind: str, message: str, **fields) -> None:
    """Structured alert: a JSON line to the alert log + a stdout log line.
    Bundle 8 will tail the file. Never raises (alerting must not crash the loop)."""
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "message": message,
    }
    rec.update(fields)
    line = json.dumps(rec, default=str)
    logger.warning("ALERT %s: %s %s", kind, message, fields if fields else "")
    try:
        with open(ALERT_LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception as e:
        logger.error("alert: could not write alert log %s: %s", ALERT_LOG_PATH, e)


# ─────────────────────── on-chain reads (wei precision) ───────────────────────

_ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "a", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"constant": True, "inputs": [],
     "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]
_VSP_CAP_ABI = [
    {"constant": True, "inputs": [],
     "name": "maxAllowedSupply", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]
_VSP_MINT_BURN_ABI = [
    {"inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "mint", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "amount", "type": "uint256"}],
     "name": "burn", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]
_ERC20_TRANSFER_ABI = [
    {"inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}],
     "name": "transfer", "outputs": [{"name": "", "type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
]

USDC_DECIMALS = 6
VSP_DECIMALS = 18


class Chain:
    """Thin wrapper over the treasury_wallet w3 instance for the reads/writes
    this worker needs. Importing treasury_wallet runs its startup assertion
    (key derives the expected worker address)."""

    def __init__(self):
        import treasury_wallet  # noqa: F401 — triggers startup assertion
        self.tw = treasury_wallet
        self.w3 = treasury_wallet.w3
        self.vsp = self.w3.eth.contract(
            address=Web3.to_checksum_address(VSP_TOKEN_ADDRESS),
            abi=_ERC20_ABI + _VSP_CAP_ABI + _VSP_MINT_BURN_ABI,
        )
        self.usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS),
            abi=_ERC20_ABI + _ERC20_TRANSFER_ABI,
        )
        self.mm = Web3.to_checksum_address(MM_ADDRESS)

    # reads (wei / micro)
    def mm_vsp_wei(self) -> int:
        return int(self.vsp.functions.balanceOf(self.mm).call())

    def mm_usdc_micro(self) -> int:
        return int(self.usdc.functions.balanceOf(self.mm).call())

    def vsp_total_supply_wei(self) -> int:
        return int(self.vsp.functions.totalSupply().call())

    def vsp_max_allowed_wei(self) -> int:
        return int(self.vsp.functions.maxAllowedSupply().call())

    # writes
    def mint_vsp(self, to: str, amount_wei: int) -> str:
        tx = self.vsp.functions.mint(
            Web3.to_checksum_address(to), int(amount_wei)
        ).build_transaction({
            "from": self.tw.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.tw.account.address, "pending"),
        })
        return self.tw.sign_and_send(tx)

    def burn_vsp(self, amount_wei: int) -> str:
        tx = self.vsp.functions.burn(int(amount_wei)).build_transaction({
            "from": self.tw.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.tw.account.address, "pending"),
        })
        return self.tw.sign_and_send(tx)

    def transfer_usdc(self, to: str, amount_micro: int) -> str:
        tx = self.usdc.functions.transfer(
            Web3.to_checksum_address(to), int(amount_micro)
        ).build_transaction({
            "from": self.tw.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.tw.account.address, "pending"),
        })
        return self.tw.sign_and_send(tx)


# ─────────────────────── cached cap + floor ───────────────────────

class TtlCache:
    def __init__(self, ttl_sec: int):
        self.ttl = ttl_sec
        self._val = None
        self._at = 0.0

    def get(self, producer):
        now = time.time()
        if self._val is None or (now - self._at) >= self.ttl:
            self._val = producer()
            self._at = now
        return self._val

    def invalidate(self):
        self._val = None


def read_floor_price() -> float:
    """Floor price (USDC per VSP) from the app's /api/mm/floor. Returns a float.
    Raises on failure; caller decides whether sweep proceeds (we skip sweep if
    floor is unavailable, to avoid an ill-sized target)."""
    import urllib.request
    url = APP_API_BASE.rstrip("/") + "/api/mm/floor"
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    # The endpoint returns the floor under one of these keys depending on
    # version; be tolerant. The current app uses 'floor_price_usd'.
    for k in ("floor_price_usd", "floor", "floor_price", "floor_usdc_per_vsp", "sell_p", "price"):
        if k in data and data[k] is not None:
            return float(data[k])
    raise RuntimeError(f"/api/mm/floor returned no recognizable floor key: {list(data.keys())}")


# ─────────────────────── band math (wei precision) ───────────────────────

WEI = 10 ** VSP_DECIMALS
MICRO = 10 ** USDC_DECIMALS


def compute_vsp_bands(cap_wei: int) -> dict:
    """Self-scaling VSP band from the on-chain cap.
        target = max(1000, min(0.1 * cap_vsp, 100000))  VSP
    All returned values are in wei. Floor/ceiling constants are configurable."""
    frac = env_float("MM_VSP_BAND_TARGET_CAP_FRACTION", 0.10)
    floor_vsp = env_float("MM_VSP_BAND_TARGET_FLOOR", 1000.0)
    ceil_vsp = env_float("MM_VSP_BAND_TARGET_MAX", 100000.0)
    min_mult = env_float("MM_VSP_BAND_MIN_MULT", 0.5)
    max_mult = env_float("MM_VSP_BAND_MAX_MULT", 2.0)

    # Compute target in VSP (float ok for band *thresholds*; the MINT AMOUNT is
    # computed separately in wei). Convert to wei via integer rounding.
    cap_vsp = cap_wei / WEI
    target_vsp = max(floor_vsp, min(frac * cap_vsp, ceil_vsp))

    target_wei = int(target_vsp * WEI)
    return {
        "target_vsp": target_vsp,
        "target_wei": target_wei,
        "band_min_wei": int(min_mult * target_wei),
        "band_max_wei": int(max_mult * target_wei),
        "burn_target_wei": target_wei,
    }


def compute_usdc_bands(target_vsp: float, floor_price: float) -> dict:
    """USDC sweep band derived from the VSP liquidity need via floor price,
    capped by the absolute custody ceiling. Values in micro-USDC."""
    abs_max = env_float("MM_USDC_TARGET_ABS_MAX", 100000.0)
    hot_max_mult = env_float("MM_USDC_HOT_MAX_MULT", 2.0)
    usdc_target = min(target_vsp * floor_price, abs_max)
    return {
        "usdc_target_usd": usdc_target,
        "usdc_target_micro": int(usdc_target * MICRO),
        "usdc_hot_max_micro": int(hot_max_mult * usdc_target * MICRO),
    }


# ─────────────────────── one iteration ───────────────────────

def run_once(chain: Chain, cap_cache: TtlCache, floor_cache: TtlCache,
             startup_treasury: str, dry_run: bool) -> None:
    master = env_bool("MM_TREASURY_AUTOMATION_ENABLED", False)
    if not master:
        logger.info("heartbeat: automation disabled at master switch")
        return

    sweep_on = env_bool("MM_TREASURY_SWEEP_ENABLED", False)
    mint_on = env_bool("MM_TREASURY_MINT_ENABLED", False)
    burn_on = env_bool("MM_TREASURY_BURN_ENABLED", False)
    dust_wei = int(env_float("MM_TREASURY_MINT_DUST", 1.0) * WEI)
    sweep_cap_micro = int(env_float("MM_USDC_SWEEP_CAP_PER_CALL", 10000.0) * MICRO)

    # ── reads ──
    try:
        mm_vsp = chain.mm_vsp_wei()
        mm_usdc = chain.mm_usdc_micro()
        total_supply = chain.vsp_total_supply_wei()
        cap_wei = cap_cache.get(chain.vsp_max_allowed_wei)
    except Exception as e:
        alert("rpc_error", f"balance/cap read failed: {e}")
        return

    bands = compute_vsp_bands(cap_wei)
    headroom = cap_wei - total_supply
    logger.info(
        "state: mm_vsp=%.4f VSP mm_usdc=%.2f USDC total_supply=%.4f cap=%.4f "
        "headroom=%.4f target=%.2f band[%.2f,%.2f] switches[m=%s s=%s mint=%s burn=%s]",
        mm_vsp / WEI, mm_usdc / MICRO, total_supply / WEI, cap_wei / WEI,
        headroom / WEI, bands["target_vsp"], bands["band_min_wei"] / WEI,
        bands["band_max_wei"] / WEI, master, sweep_on, mint_on, burn_on,
    )

    # ── SWEEP USDC if too much in MM ──
    if sweep_on:
        try:
            floor_price = floor_cache.get(read_floor_price)
        except Exception as e:
            alert("floor_unavailable", f"sweep skipped: floor price read failed: {e}")
            floor_price = None
        if floor_price is not None:
            usdc_bands = compute_usdc_bands(bands["target_vsp"], floor_price)
            if mm_usdc > usdc_bands["usdc_hot_max_micro"]:
                # destination guard (lesson: refuse if changed since startup)
                current_treasury = os.getenv("TREASURY_ADDRESS", TREASURY_ADDRESS)
                if current_treasury.lower() != startup_treasury.lower():
                    alert("destination_mismatch",
                          "TREASURY_ADDRESS changed since startup; exiting",
                          startup=startup_treasury, current=current_treasury)
                    sys.exit(1)
                excess = mm_usdc - usdc_bands["usdc_target_micro"]
                amount = min(excess, sweep_cap_micro)
                if amount > 0:
                    if dry_run:
                        alert("dry_run_sweep",
                              f"would sweep {amount / MICRO:.2f} USDC -> {startup_treasury}",
                              amount_usdc=amount / MICRO, floor=floor_price,
                              usdc_target=usdc_bands["usdc_target_usd"])
                    else:
                        try:
                            tx = chain.transfer_usdc(startup_treasury, amount)
                            alert("sweep", f"swept {amount / MICRO:.2f} USDC -> TREASURY",
                                  amount_usdc=amount / MICRO, tx=tx,
                                  floor=floor_price,
                                  usdc_target=usdc_bands["usdc_target_usd"])
                        except chain.tw.TxRevertedError as e:
                            alert("sweep_reverted", f"sweep tx reverted: {e}", tx_hash=e.tx_hash)
                        except Exception as e:
                            alert("sweep_failed", f"sweep failed: {e}")

    # ── MINT VSP if MM too low ──
    if mint_on and mm_vsp < bands["band_min_wei"]:
        desired = bands["target_wei"] - mm_vsp
        if desired > dust_wei:
            if headroom <= dust_wei:
                alert("mint_cap_binding",
                      f"cap binding: headroom {headroom / WEI:.4f} VSP <= dust; skipping mint",
                      headroom_vsp=headroom / WEI, cap_vsp=cap_wei / WEI,
                      total_supply_vsp=total_supply / WEI)
            else:
                amount = min(desired, headroom)
                if amount <= dust_wei:
                    alert("mint_shaved_to_dust",
                          f"mint would shave to dust ({amount / WEI:.4f} VSP); skipping",
                          desired_vsp=desired / WEI, headroom_vsp=headroom / WEI)
                elif dry_run:
                    shaved = amount < desired
                    alert("dry_run_mint",
                          f"would mint {amount / WEI:.4f} VSP -> MM"
                          + (" (SHAVED)" if shaved else ""),
                          amount_vsp=amount / WEI, desired_vsp=desired / WEI,
                          shaved=shaved, headroom_vsp=headroom / WEI)
                else:
                    try:
                        tx = chain.mint_vsp(MM_ADDRESS, amount)
                        if amount < desired:
                            alert("mint_shaved",
                                  f"minted {amount / WEI:.4f} VSP (shaved from "
                                  f"{desired / WEI:.4f}; cap binding) -> MM",
                                  amount_vsp=amount / WEI, desired_vsp=desired / WEI,
                                  headroom_vsp=headroom / WEI, tx=tx)
                        else:
                            alert("mint", f"minted {amount / WEI:.4f} VSP -> MM",
                                  amount_vsp=amount / WEI, tx=tx)
                        # totalSupply just changed; invalidate cap cache so the
                        # next cycle recomputes headroom from a fresh cap if its
                        # TTL is still warm (cap itself didn't change, but being
                        # conservative is cheap).
                    except chain.tw.TxRevertedError as e:
                        # Most likely cap-exceeded due to a race; alert + retry.
                        alert("mint_reverted",
                              f"mint reverted (cap race?): {e}", tx_hash=e.tx_hash,
                              amount_vsp=amount / WEI)
                    except Exception as e:
                        alert("mint_failed", f"mint failed: {e}", amount_vsp=amount / WEI)

    # ── BURN VSP if MM too high ──
    if burn_on and mm_vsp > bands["band_max_wei"]:
        amount = mm_vsp - bands["burn_target_wei"]
        if amount > dust_wei:
            if dry_run:
                alert("dry_run_burn", f"would burn {amount / WEI:.4f} VSP from MM",
                      amount_vsp=amount / WEI)
            else:
                try:
                    tx = chain.burn_vsp(amount)
                    alert("burn", f"burned {amount / WEI:.4f} VSP from MM",
                          amount_vsp=amount / WEI, tx=tx)
                except chain.tw.TxRevertedError as e:
                    alert("burn_reverted", f"burn reverted: {e}", tx_hash=e.tx_hash)
                except Exception as e:
                    alert("burn_failed", f"burn failed: {e}")


# ─────────────────────── main ───────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="VSP treasury automation worker")
    ap.add_argument("--dry-run", action="store_true",
                    help="read + compute + log intended actions, sign nothing")
    ap.add_argument("--once", action="store_true",
                    help="run a single iteration and exit")
    args = ap.parse_args()

    print("=== VSP Treasury Worker ===")
    logger.info("startup: dry_run=%s once=%s", args.dry_run, args.once)

    # Startup: instantiate chain (runs treasury_wallet key->address assertion).
    try:
        chain = Chain()
    except Exception as e:
        logger.error("startup failed: %s", e)
        # Fail loud: a bad key or RPC means the container should not pretend to run.
        sys.exit(1)

    startup_treasury = os.getenv("TREASURY_ADDRESS", TREASURY_ADDRESS)
    if not startup_treasury or startup_treasury.lower() == MM_ADDRESS.lower():
        alert("config_error",
              "TREASURY_ADDRESS unset or equals MM_ADDRESS; sweeps would be a no-op/self-send",
              treasury=startup_treasury, mm=MM_ADDRESS)
        # Not fatal (mint/burn still work), but loudly flagged.

    interval = env_int("MM_TREASURY_INTERVAL_SEC", 600)
    cap_ttl = env_int("MM_TREASURY_CAP_CACHE_TTL_SEC", 3600)
    floor_ttl = env_int("MM_TREASURY_FLOOR_CACHE_TTL_SEC", 3600)
    cap_cache = TtlCache(cap_ttl)
    floor_cache = TtlCache(floor_ttl)

    logger.info(
        "config: interval=%ss cap_ttl=%ss floor_ttl=%ss treasury=%s worker=%s",
        interval, cap_ttl, floor_ttl, startup_treasury, chain.tw.account.address,
    )

    if args.once:
        run_once(chain, cap_cache, floor_cache, startup_treasury, args.dry_run)
        return

    # patch_bundle10d_compose_hardening_tw: heartbeat-file touch for the compose healthcheck.
    # Touched at the START of every iteration so the worker is reported
    # healthy even when run_once raises into the loop_error alert path.
    # 900s healthcheck threshold accommodates the default 600s interval.
    from pathlib import Path as _HB_Path
    _HB_FILE = _HB_Path("/tmp/treasury_worker.heartbeat")
    while True:
        try:
            try:
                _HB_FILE.touch()
            except Exception as _hb_err:
                logger.warning("treasury heartbeat touch failed: %s", _hb_err)
            run_once(chain, cap_cache, floor_cache, startup_treasury, args.dry_run)
        except SystemExit:
            raise
        except Exception as e:
            alert("loop_error", f"unhandled error in iteration: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
