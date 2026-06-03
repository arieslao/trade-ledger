-- trade-ledger schema migration 001
-- Single canonical trades table for the trade lifecycle ledger.

CREATE TABLE IF NOT EXISTS trades (
    trade_id            TEXT PRIMARY KEY,           -- generate_trade_id(method, symbol)

    -- Identity + sizing
    method              TEXT NOT NULL,
    asset_class         TEXT NOT NULL CHECK (asset_class IN ('options','equity','crypto','forex')),
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    qty                 NUMERIC NOT NULL,
    contract            JSONB,                       -- single-leg or multi-leg spread

    -- Entry
    entry_ts            TIMESTAMPTZ NOT NULL,
    entry_price         NUMERIC NOT NULL,
    entry_price_source  TEXT NOT NULL CHECK (entry_price_source IN ('fill','mid','mark','estimate')),
    entry_message_id    TEXT,                        -- optional external notification message id

    -- Risk
    stop                NUMERIC,
    target              NUMERIC,
    hard_exit_at        TEXT,                        -- e.g. ISO timestamp or 'EOD'
    confidence          NUMERIC,
    reasoning           TEXT,

    -- Exit (filled at close_trade)
    exit_ts             TIMESTAMPTZ,
    exit_price          NUMERIC,
    exit_price_source   TEXT CHECK (exit_price_source IS NULL
                                    OR exit_price_source IN ('fill','mid','mark','estimate')),
    exit_reason         TEXT,
    exit_message_id     TEXT,

    -- P&L (always derived together by compute_pnl)
    pnl_dollars         NUMERIC,
    pnl_pct             NUMERIC,
    pnl_source          TEXT CHECK (pnl_source IS NULL
                                    OR pnl_source IN ('broker','computed','estimate')),

    -- Lifecycle state
    status              TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','closed','partial','expired','orphaned')),
    mode                TEXT NOT NULL DEFAULT 'live'
                        CHECK (mode IN ('live','shadow','paper')),
    confidence_tier     TEXT NOT NULL DEFAULT 'native'
                        CHECK (confidence_tier IN ('native','reconstructed','estimate')),
    policy_version      TEXT NOT NULL DEFAULT 'v1',

    -- Free-form
    metadata            JSONB,

    -- Auditing
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS trades_method_idx   ON trades(method);
CREATE INDEX IF NOT EXISTS trades_status_idx   ON trades(status);
CREATE INDEX IF NOT EXISTS trades_entry_ts_idx ON trades(entry_ts);
CREATE INDEX IF NOT EXISTS trades_symbol_idx   ON trades(symbol);
CREATE INDEX IF NOT EXISTS trades_mode_idx     ON trades(mode);

-- For paired shadow lookups (shadow.metadata->>'shadow_pair_of' = live.trade_id)
CREATE INDEX IF NOT EXISTS trades_shadow_pair_idx
    ON trades ((metadata->>'shadow_pair_of'))
    WHERE mode = 'shadow';
