-- ============================================================
-- VALIDATION QUERIES
-- Run these AFTER building all models. Each SELECT lets you eyeball
-- that a hard model produced the reshaping the gold edge file claims.
-- This is how the gold labels get CERTIFIED (executed, not assumed).
-- Expected shapes are described in each comment.
-- ============================================================

USE DATABASE OMNIMART_BENCH;
USE SCHEMA MART;

-- T7 no-RI merge: expect 9 rows (5 web + 4 mkt), web rows have NULL
-- seller_name/seller_region, mkt rows have seller_name populated via
-- the inferred seller_code join.
SELECT source_system, order_natural_key, seller_code, seller_name, seller_region
FROM stg_orders_unified
ORDER BY source_system, order_natural_key;

-- FLATTEN: expect one row per event with JSON paths pulled into
-- customer_ident / event_type / product_sku / dwell_seconds.
SELECT * FROM stg_events_flat ORDER BY event_id;

-- T6 derived calc: confirm running_ltv is monotonic within a customer
-- ordered by datetime, value_tier follows the CASE thresholds.
SELECT customer_ident, order_datetime, order_amount,
       running_ltv, recency_rank, value_tier, amount_vs_cust_avg
FROM dim_customer_metrics
ORDER BY customer_ident, order_datetime;

-- T5a static pivot: expect one row per country, three revenue columns
-- rev_2024_01..rev_2024_03, each summing order_amount for that month.
SELECT * FROM fct_sales_wide_static ORDER BY country;

-- T5b dynamic pivot: output columns are the DISTINCT product_category
-- values (Widgets/Gadgets/Gizmos) -- names come from the DATA, not the
-- SQL text. Confirm the column headers match categories present.
SELECT * FROM fct_category_pivot_dyn;

-- Nested pivot->derived scorecard: expect per-seller pivoted country
-- revenue plus total_rev (sum of the four) and market_flag.
SELECT * FROM mart_seller_scorecard ORDER BY seller_code;

-- CTE chain summary.
SELECT * FROM rpt_exec_summary ORDER BY customer_ident;

-- ============================================================
-- DP LAYER validation (archetypes A and B)
-- ============================================================

-- DP-A wide rename: confirm every family's columns are populated and
-- NOT cross-wired (buyer_city != payer_city values, etc.).
SELECT buyer_city, shipto_city, payer_city, household_city FROM dp_party_360;
SELECT * FROM dp_item_catalog;

-- DP-B calc-heavy: spot-check a derived value against hand math.
-- rebate_amount should equal qualifying_purchase_amount * rebate_rate.
SELECT agreement_key, qualifying_purchase_amount, rebate_rate,
       rebate_amount, margin_amount, margin_pct, payment_due_amount,
       make_whole_amount, effective_rate, evaluation_days
FROM dp_settlement_summary ORDER BY agreement_key;

-- net_revenue = gross - discount - return ; running_customer_revenue
-- should accumulate within a customer ordered by date.
SELECT order_key, customer_key, order_date, net_revenue,
       gross_margin_amount, gross_margin_pct, loyalty_burn_amount,
       total_landed_amount, running_customer_revenue, revenue_rank_in_customer
FROM dp_order_economics ORDER BY customer_key, order_date;
