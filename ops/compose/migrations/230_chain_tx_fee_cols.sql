-- Migration: 230_chain_tx_fee_cols.sql
--
-- Bundle 4.5 patch 2.1: action-rollup support for chain_tx.
--
-- principal_vsp: the VSP amount moved as the primary action of a
--                protocol event (e.g. for StakeAdded, the user's
--                stake principal; for PostCreated, NULL since no
--                VSP moves with claim creation itself, only fees).
--                Populated by the indexer at write time by scanning
--                Transfer events in the same tx.
--
-- fee_vsp:      the VSP amount the user paid in fees during this tx
--                (Forwarder treasury, COLD_SAFE, MM-side mechanic).
--                Summed across multiple Transfer events to internal
--                addresses in the same tx.
--
-- Both nullable. Only protocol-event rows (StakeAdded /
-- StakeWithdrawn / PostCreated / EdgeAdded) populate them. Transfer
-- rows that survive the tightened filter (genuine user-to-user
-- transfers) leave both NULL — `amount_vsp` is the canonical value
-- there.

ALTER TABLE chain_tx
    ADD COLUMN IF NOT EXISTS principal_vsp DOUBLE PRECISION;
ALTER TABLE chain_tx
    ADD COLUMN IF NOT EXISTS fee_vsp       DOUBLE PRECISION;
