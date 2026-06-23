-- ============================================================================
-- v_book_summary.sql
-- Desk-facing summary view: one row per active position with security,
-- counterparty, and latest market rate joined in. This is the view the
-- Python orchestration layer's "read from persisted store" path queries
-- in production (replacing the in-memory fixtures used in this reference
-- build's tests — see api/state.py docstring).
-- ============================================================================

CREATE OR REPLACE VIEW secfi.v_book_summary AS
SELECT
    p.position_id,
    p.trade_date,
    p.maturity_date,
    p.direction,
    s.internal_id          AS security_internal_id,
    s.ticker,
    s.product_type,
    s.issuer_id,
    s.gics_sector,
    s.country_of_risk,
    c.counterparty_id,
    c.legal_name            AS counterparty_name,
    c.tier                    AS counterparty_tier,
    c.region                    AS counterparty_region,
    c.watch_list,
    p.quantity,
    p.market_value,
    p.currency,
    p.rate_bps,
    p.rate_type_is_rebate,
    p.term_type,
    p.desk_id,
    p.trader_id,
    mr.weighted_avg_fee_bps  AS latest_market_rate_bps,
    mr.utilization_pct          AS latest_market_utilization_pct,
    (p.rate_bps - mr.weighted_avg_fee_bps) AS rate_gap_bps,
    cl.limit_usd                  AS counterparty_limit_usd
FROM secfi.position p
JOIN secfi.security s        ON s.internal_id = p.security_internal_id
JOIN secfi.counterparty c    ON c.counterparty_id = p.counterparty_id
LEFT JOIN LATERAL (
    SELECT weighted_avg_fee_bps, utilization_pct
    FROM secfi.market_rate_quote mq
    WHERE mq.security_internal_id = s.internal_id
    ORDER BY mq.as_of DESC
    LIMIT 1
) mr ON TRUE
LEFT JOIN secfi.counterparty_limit cl ON cl.counterparty_id = c.counterparty_id
WHERE p.is_active = TRUE;

COMMENT ON VIEW secfi.v_book_summary IS
  'Primary desk dashboard read model. Backed by indexes on position/security/'
  'counterparty/market_rate_quote — verify EXPLAIN ANALYZE on this view '
  'after any schema change, see docs/runbook.md "Performance Checks".';
