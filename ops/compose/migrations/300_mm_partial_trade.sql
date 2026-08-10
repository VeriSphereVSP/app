-- Migration: 300_mm_partial_trade.sql
-- F-7 (LAUNCH-RISK-AUDIT 2026-07-06 §2.1): MM trades are multi-leg and NON-ATOMIC.
-- A buy is (user USDC -> MM) then (MM VSP -> user); a sell is (user VSP -> MM)
-- then (MM USDC -> user). If an intermediate leg lands but a later leg fails
-- (RPC drop, receipt timeout, MM gas exhaustion, process kill), funds have moved
-- one way with no counter-move and nothing recorded it. This table is the durable
-- record: the money routes write a row the instant ANY leg moves on-chain, and the
-- reconciler (worker) reads it to complete-forward or refund-back, idempotently.
--
-- Lifecycle of `state`:
--   open        -> at least one leg landed; cure not yet attempted
--   reconciled  -> reconciler verified on-chain and the trade is now whole
--                  (either the missing leg was completed, or it turned out the
--                   "failed" leg had actually landed after a timeout)
--   refunded    -> reconciler returned the moved funds to the user
--   failed      -> reconciler could not cure (e.g. MM insolvent for the cure leg);
--                  left for a human. Alerts fire on entry to this state.
--   noop        -> no leg actually moved on-chain (the request failed before any
--                   transfer, or all legs were confirmed absent); closed harmlessly.

CREATE TABLE IF NOT EXISTS mm_partial_trade (
  id                BIGSERIAL PRIMARY KEY,
  side              TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
  user_address      TEXT NOT NULL,
  qty_vsp           DOUBLE PRECISION NOT NULL,
  -- planned amounts (what a whole trade would move), micro-USDC / wei as noted
  reserves_micro    BIGINT NOT NULL DEFAULT 0,   -- USDC principal leg
  fee_micro         BIGINT NOT NULL DEFAULT 0,   -- USDC fee leg (buy only)
  vsp_wei           NUMERIC(78,0) NOT NULL DEFAULT 0,  -- VSP leg
  -- which legs are CONFIRMED landed on-chain, with their hashes (NULL = not landed)
  leg_principal_in_tx  TEXT,   -- buy: user USDC->MM ; sell: user VSP->MM
  leg_fee_in_tx        TEXT,   -- buy: user USDC->treasury (fee) ; sell: MM USDC fee->treasury
  leg_payout_out_tx    TEXT,   -- buy: MM VSP->user ; sell: MM USDC->user
  -- reconciliation
  state             TEXT NOT NULL DEFAULT 'open'
                      CHECK (state IN ('open','reconciled','refunded','failed','noop')),
  cure_tx           TEXT,           -- the hash of the completing/refunding tx, if any
  cure_kind         TEXT,           -- 'complete_forward' | 'refund_back' | 'already_whole' | 'no_move'
  attempts          INT NOT NULL DEFAULT 0,
  last_error        TEXT,
  -- idempotency: one row per (user, side, planned VSP qty, minute-bucket). The money
  -- route computes this deterministically so a retried request can't double-insert.
  idem_key          TEXT NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mm_partial_idem ON mm_partial_trade (idem_key);
CREATE INDEX IF NOT EXISTS idx_mm_partial_state ON mm_partial_trade (state);
CREATE INDEX IF NOT EXISTS idx_mm_partial_user ON mm_partial_trade (user_address);
CREATE INDEX IF NOT EXISTS idx_mm_partial_created ON mm_partial_trade (created_at);
