-- ============================================================
-- GOLD LAYER : marts / reporting
-- The hardest lineage cases live here: PIVOT (static + dynamic),
-- window/CASE derived calculations, and a nested pivot->derived
-- scorecard. These are the cells where we expect static parsers to
-- degrade and the LLM arm to behave differently.
-- ============================================================

USE DATABASE OMNIMART_BENCH;
USE SCHEMA MART;

-- ============================================================
-- dim_customer_metrics  [T6 : derived calc, window + CASE]  -- HARD
-- Many-to-one, expression-level lineage: each output metric derives
-- from one or more source columns through window functions and CASE
-- arithmetic. running_ltv depends on order_amount ordered by datetime;
-- value_tier depends on order_amount via CASE thresholds; recency_rank
-- depends on order_datetime.
-- ============================================================
CREATE OR REPLACE TABLE dim_customer_metrics AS
SELECT
    customer_ident,
    source_system,
    order_datetime,
    order_amount,
    SUM(order_amount) OVER (
        PARTITION BY customer_ident
        ORDER BY order_datetime
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )                                          AS running_ltv,
    ROW_NUMBER() OVER (
        PARTITION BY customer_ident
        ORDER BY order_datetime DESC
    )                                          AS recency_rank,
    CASE
        WHEN order_amount >= 300 THEN 'HIGH'
        WHEN order_amount >= 100 THEN 'MID'
        ELSE 'LOW'
    END                                        AS value_tier,
    order_amount
      - AVG(order_amount) OVER (PARTITION BY customer_ident) AS amount_vs_cust_avg
FROM stg_orders_unified;


-- ============================================================
-- fct_sales_wide_static  [T5a : static PIVOT, fixed IN-list]  -- HARD
-- Row->column reshaping with an explicit month IN-list. Each output
-- column (rev_2024_01 ... rev_2024_03) traces to the SAME source
-- measure (order_amount) filtered by the pivot key (order month).
-- One source column fans out to N output columns -- the canonical
-- hard case for column lineage.
-- ============================================================
CREATE OR REPLACE TABLE fct_sales_wide_static AS
SELECT *
FROM (
    SELECT
        country,
        TO_VARCHAR(order_datetime, 'YYYY-MM') AS order_month,
        order_amount
    FROM stg_orders_unified
)
PIVOT (
    SUM(order_amount)
    FOR order_month IN ('2024-01', '2024-02', '2024-03')
) AS p (country, rev_2024_01, rev_2024_02, rev_2024_03);


-- ============================================================
-- fct_category_pivot_dyn  [T5b : dynamic PIVOT, ANY]  -- HARDEST
-- Uses PIVOT ... IN (ANY), where the output columns are NOT
-- enumerated in the SQL text at all -- they are determined at run
-- time by the distinct values of product_category. Static parsers
-- cannot know the output column names from the text alone; this is
-- the sharpest degradation case. Gold treats every dynamically
-- produced revenue column as tracing to line_total (filtered by
-- product_category).
-- ============================================================
CREATE OR REPLACE TABLE fct_category_pivot_dyn AS
SELECT *
FROM (
    SELECT
        order_key,
        product_category,
        line_total
    FROM stg_order_items_enriched
)
PIVOT (
    SUM(line_total)
    FOR product_category IN (ANY ORDER BY product_category)
);


-- ============================================================
-- mart_seller_scorecard  [T5 + T6 nested : pivot -> derived]  -- HARDEST
-- A CTE first pivots seller revenue by country (row->col), then a
-- downstream SELECT applies derived CASE + arithmetic ON the pivoted
-- columns. Lineage must thread through the pivot AND the subsequent
-- expression layer: total_rev derives from the pivoted country cols,
-- which each derive from order_amount.
-- ============================================================
CREATE OR REPLACE TABLE mart_seller_scorecard AS
WITH seller_country_pivot AS (
    SELECT *
    FROM (
        SELECT
            seller_code,
            country,
            order_amount
        FROM stg_orders_unified
        WHERE seller_code IS NOT NULL
    )
    PIVOT (
        SUM(order_amount)
        FOR country IN ('US', 'DE', 'GB', 'CA')
    ) AS p (seller_code, rev_us, rev_de, rev_gb, rev_ca)
)
SELECT
    scp.seller_code,
    scp.rev_us,
    scp.rev_de,
    scp.rev_gb,
    scp.rev_ca,
    COALESCE(scp.rev_us,0) + COALESCE(scp.rev_de,0)
      + COALESCE(scp.rev_gb,0) + COALESCE(scp.rev_ca,0)   AS total_rev,
    CASE
        WHEN COALESCE(scp.rev_de,0) + COALESCE(scp.rev_gb,0) > 0
        THEN 'HAS_EU'
        ELSE 'NA_ONLY'
    END                                                   AS market_flag
FROM seller_country_pivot scp;


-- ============================================================
-- rpt_exec_summary  [T4 : CTE chain over marts]  -- MED
-- A multi-CTE query that reads from stg_sales_base and
-- dim_customer_metrics, joins, and projects. Exercises CTE / nested
-- intermediate-result lineage without new reshaping.
-- ============================================================
CREATE OR REPLACE VIEW rpt_exec_summary AS
WITH cust_roll AS (
    SELECT
        customer_ident,
        MAX(running_ltv)   AS final_ltv,
        MIN(value_tier)    AS best_tier
    FROM dim_customer_metrics
    GROUP BY customer_ident
),
sales_roll AS (
    SELECT
        order_key,
        order_line_revenue
    FROM stg_sales_base
)
SELECT
    c.customer_ident,
    c.final_ltv,
    c.best_tier,
    s.order_line_revenue
FROM cust_roll c
LEFT JOIN sales_roll s
    ON c.customer_ident = s.order_key;   -- deliberately weak/soft join
