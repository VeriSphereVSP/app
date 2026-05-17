-- Migration: 031_mm_state_drop_vestigial.sql
--
-- Drops three mm_state columns that are now derived from chain:
--   net_vsp          - equal to (totalSupply - balanceOf(MM)) of VSPToken
--   usdc_reserves    - equal to balanceOf(MM) + Σ balanceOf(cold safes) of USDC
--   vsp_circulating  - equal to (totalSupply - balanceOf(MM)) of VSPToken
--
-- Pricing reads these directly from chain via read_vsp_circulating() and
-- read_usdc_reserves(). The DB columns were maintained as a "forensic record"
-- but drifted from chain (observed: 24 USDC and ~12 VSP drift with no recent
-- activity) and were never read by pricing — they had no consumers.
--
-- The n<0 reserve-price branch in mm_pricing.py is unreachable in the current
-- accounting model (confirmed 2026-05-15), so net_vsp adds no information
-- beyond vsp_circulating. Keep unit_au, half_spread (parameters) and
-- fees_collected_usdc (counter).
--
-- mm_trade.{net_vsp_before,net_vsp_after,usdc_reserves_after,vsp_circulating_after}
-- are NOT dropped — that table is an immutable audit log, and post-trade values
-- there will be written from post-trade chain reads going forward (mm_routes.py
-- _log_trade change).
--
-- Idempotent: uses IF EXISTS so re-runs are no-ops.

ALTER TABLE mm_state DROP COLUMN IF EXISTS net_vsp;
ALTER TABLE mm_state DROP COLUMN IF EXISTS usdc_reserves;
ALTER TABLE mm_state DROP COLUMN IF EXISTS vsp_circulating;
