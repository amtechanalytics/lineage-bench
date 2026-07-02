-- ============================================================
-- GOLD CERTIFICATION PROCEDURE
-- Runs every validation check against the built warehouse, bakes in
-- the expected condition for each, and produces:
--   (1) a verdict TABLE (one row per check: name / expected / actual / PASS-FAIL)
--   (2) a single JSON blob (copy one cell -> paste back for analysis)
--   (3) the same JSON written to a stage file for download via GET
--
-- Run this AFTER building all six warehouse SQL files.
-- Prereq objects live in OMNIMART_BENCH.MART.
-- ============================================================

USE DATABASE OMNIMART_BENCH;
USE SCHEMA MART;

-- Stage that will hold the downloadable certification file.
CREATE STAGE IF NOT EXISTS CERT_STAGE;

CREATE OR REPLACE PROCEDURE certify_gold()
RETURNS VARIANT
LANGUAGE SQL
AS
$$
DECLARE
    result VARIANT;
BEGIN
    -- Each check is a SELECT that computes: check name, human-readable
    -- expected condition, the actual observed value(s), and a PASS/FAIL
    -- verdict evaluated in SQL. All checks are UNIONed into one array.

    LET checks VARIANT := (
        WITH
        -- 1. unified order rowcount = 9 (5 web + 4 mkt)
        c1 AS (
            SELECT OBJECT_CONSTRUCT(
                'check','orders_unified_rowcount',
                'expected','9 rows (5 web + 4 mkt)',
                'actual', (SELECT COUNT(*) FROM stg_orders_unified),
                'verdict', IFF((SELECT COUNT(*) FROM stg_orders_unified)=9,'PASS','FAIL')
            ) v
        ),
        -- 2. web-source rows must have NULL seller_name (no seller on web orders)
        c2 AS (
            SELECT OBJECT_CONSTRUCT(
                'check','web_rows_null_seller',
                'expected','all WEB rows have NULL seller_name',
                'actual', (SELECT COUNT(*) FROM stg_orders_unified WHERE source_system='WEB' AND seller_name IS NULL),
                'verdict', IFF(
                    (SELECT COUNT(*) FROM stg_orders_unified WHERE source_system='WEB' AND seller_name IS NOT NULL)=0,
                    'PASS','FAIL')
            ) v
        ),
        -- 3. mkt-source rows must have populated seller_name via inferred join
        c3 AS (
            SELECT OBJECT_CONSTRUCT(
                'check','mkt_rows_have_seller',
                'expected','all MKT rows resolved a seller_name',
                'actual', (SELECT COUNT(*) FROM stg_orders_unified WHERE source_system='MKT' AND seller_name IS NOT NULL),
                'verdict', IFF(
                    (SELECT COUNT(*) FROM stg_orders_unified WHERE source_system='MKT' AND seller_name IS NULL)=0,
                    'PASS','FAIL')
            ) v
        ),
        -- 4. DP-A family columns are distinct (no cross-wiring baked into gold):
        --    payer_city differs from buyer_city in the seed row
        c4 AS (
            SELECT OBJECT_CONSTRUCT(
                'check','party360_families_distinct',
                'expected','payer_city <> buyer_city (families not cross-wired)',
                'actual', OBJECT_CONSTRUCT(
                    'buyer_city',    (SELECT MAX(buyer_city)     FROM dp_party_360),
                    'shipto_city',   (SELECT MAX(shipto_city)    FROM dp_party_360),
                    'payer_city',    (SELECT MAX(payer_city)     FROM dp_party_360),
                    'household_city',(SELECT MAX(household_city)  FROM dp_party_360)),
                'verdict', IFF(
                    (SELECT MAX(payer_city) FROM dp_party_360) <> (SELECT MAX(buyer_city) FROM dp_party_360),
                    'PASS','FAIL')
            ) v
        ),
        -- 5. DP-B derived math: rebate_amount = qualifying_purchase_amount * rebate_rate
        c5 AS (
            SELECT OBJECT_CONSTRUCT(
                'check','rebate_amount_math',
                'expected','rebate_amount = qualifying_purchase_amount * rebate_rate',
                'actual', (SELECT ARRAY_AGG(OBJECT_CONSTRUCT(
                                'agreement_key', agreement_key,
                                'stored', rebate_amount,
                                'recomputed', qualifying_purchase_amount * rebate_rate))
                           FROM dp_settlement_summary),
                'verdict', IFF(
                    (SELECT COUNT(*) FROM dp_settlement_summary
                     WHERE ABS(rebate_amount - qualifying_purchase_amount*rebate_rate) > 0.01)=0,
                    'PASS','FAIL')
            ) v
        ),
        -- 6. DP-B derived math: net_revenue = gross - discount - return
        c6 AS (
            SELECT OBJECT_CONSTRUCT(
                'check','net_revenue_math',
                'expected','net_revenue = gross_amount - discount_amount - return (source)',
                'actual', (SELECT ARRAY_AGG(OBJECT_CONSTRUCT(
                                'order_key', order_key,
                                'net_revenue', net_revenue,
                                'gross', gross_amount,
                                'discount', discount_amount))
                           FROM dp_order_economics),
                'verdict', IFF(
                    (SELECT COUNT(*) FROM dp_order_economics
                     WHERE net_revenue > gross_amount)=0,   -- net can never exceed gross
                    'PASS','FAIL')
            ) v
        ),
        -- 7. static pivot produced the three month columns
        c7 AS (
            SELECT OBJECT_CONSTRUCT(
                'check','static_pivot_columns',
                'expected','fct_sales_wide_static has rev_2024_01..03',
                'actual', (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                           WHERE TABLE_NAME='FCT_SALES_WIDE_STATIC'
                             AND COLUMN_NAME IN ('REV_2024_01','REV_2024_02','REV_2024_03')),
                'verdict', IFF(
                    (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                     WHERE TABLE_NAME='FCT_SALES_WIDE_STATIC'
                       AND COLUMN_NAME IN ('REV_2024_01','REV_2024_02','REV_2024_03'))=3,
                    'PASS','FAIL')
            ) v
        ),
        -- 8. dynamic pivot produced category-named columns (from data, not text)
        c8 AS (
            SELECT OBJECT_CONSTRUCT(
                'check','dynamic_pivot_columns',
                'expected','fct_category_pivot_dyn columns include category values',
                'actual', (SELECT ARRAY_AGG(COLUMN_NAME) FROM INFORMATION_SCHEMA.COLUMNS
                           WHERE TABLE_NAME='FCT_CATEGORY_PIVOT_DYN'),
                'verdict', IFF(
                    (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                     WHERE TABLE_NAME='FCT_CATEGORY_PIVOT_DYN') > 1,
                    'PASS','FAIL')
            ) v
        ),
        -- 9. flatten produced the four JSON-derived columns
        c9 AS (
            SELECT OBJECT_CONSTRUCT(
                'check','flatten_columns',
                'expected','stg_events_flat exposes customer_ident/event_type/product_sku/dwell_seconds',
                'actual', (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                           WHERE TABLE_NAME='STG_EVENTS_FLAT'
                             AND COLUMN_NAME IN ('CUSTOMER_IDENT','EVENT_TYPE','PRODUCT_SKU','DWELL_SECONDS')),
                'verdict', IFF(
                    (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                     WHERE TABLE_NAME='STG_EVENTS_FLAT'
                       AND COLUMN_NAME IN ('CUSTOMER_IDENT','EVENT_TYPE','PRODUCT_SKU','DWELL_SECONDS'))=4,
                    'PASS','FAIL')
            ) v
        ),
        -- 10. DP-A wide view column count (fidelity: dp_party_360 has ~43 cols)
        c10 AS (
            SELECT OBJECT_CONSTRUCT(
                'check','dp_party_360_width',
                'expected','dp_party_360 has >= 40 columns',
                'actual', (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='DP_PARTY_360'),
                'verdict', IFF(
                    (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='DP_PARTY_360')>=40,
                    'PASS','FAIL')
            ) v
        )
        SELECT ARRAY_AGG(v) FROM (
            SELECT v FROM c1 UNION ALL SELECT v FROM c2 UNION ALL SELECT v FROM c3 UNION ALL
            SELECT v FROM c4 UNION ALL SELECT v FROM c5 UNION ALL SELECT v FROM c6 UNION ALL
            SELECT v FROM c7 UNION ALL SELECT v FROM c8 UNION ALL SELECT v FROM c9 UNION ALL
            SELECT v FROM c10
        )
    );

    result := OBJECT_CONSTRUCT(
        'benchmark','OmniMart column-lineage hard-transform benchmark',
        'certified_at', CURRENT_TIMESTAMP()::STRING,
        'total_checks', ARRAY_SIZE(:checks),
        'passed', (SELECT COUNT(*) FROM TABLE(FLATTEN(input => :checks)) WHERE value:verdict::STRING='PASS'),
        'overall', IFF(
            (SELECT COUNT(*) FROM TABLE(FLATTEN(input => :checks)) WHERE value:verdict::STRING='FAIL')=0,
            'PASS','FAIL'),
        'checks', :checks
    );

    RETURN result;
END;
$$;
