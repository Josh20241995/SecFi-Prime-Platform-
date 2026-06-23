-- ============================================================================
-- v_counterparty_exposure_rollup.sql
-- Aggregated, parent-group-rolled-up exposure view for the counterparty
-- heatmap dashboard. Note: this is a SIMPLE SQL aggregation (gross MV by
-- side) and is NOT a substitute for the Python risk/counterparty_risk.py
-- stress-tested exposure calculation — it exists for fast dashboard
-- rendering of the non-stressed base case only. Stress scenario figures
-- must come from the orchestration cycle's computed output.
-- ============================================================================

CREATE OR REPLACE VIEW secfi.v_counterparty_exposure_rollup AS
SELECT
    COALESCE(c.parent_group_id, c.counterparty_id) AS rollup_group_id,
    c.counterparty_id,
    c.legal_name,
    c.tier,
    c.region,
    c.watch_list,
    SUM(CASE WHEN p.direction IN ('LEND','REVERSE_REPO') THEN p.market_value ELSE 0 END) AS lend_mv_usd,
    SUM(CASE WHEN p.direction IN ('BORROW','REPO') THEN p.market_value ELSE 0 END)        AS borrow_mv_usd,
    SUM(p.market_value)                                                                     AS gross_mv_usd,
    COUNT(*)                                                                                  AS position_count,
    cl.limit_usd,
    CASE WHEN cl.limit_usd > 0 THEN SUM(p.market_value) / cl.limit_usd ELSE NULL END           AS utilization_pct
FROM secfi.position p
JOIN secfi.counterparty c ON c.counterparty_id = p.counterparty_id
LEFT JOIN secfi.counterparty_limit cl ON cl.counterparty_id = c.counterparty_id
WHERE p.is_active = TRUE
GROUP BY c.parent_group_id, c.counterparty_id, c.legal_name, c.tier, c.region, c.watch_list, cl.limit_usd;
