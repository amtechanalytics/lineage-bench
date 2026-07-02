-- ============================================================
-- SILVER LAYER : conformed / staged models
-- Each file is one CREATE ... AS. Transform-type tag noted in the
-- header comment; these tags mirror the gold edge file taxonomy.
-- ============================================================

USE DATABASE OMNIMART_BENCH;
USE SCHEMA MART;

-- ============================================================
-- stg_products  [T1 : projection / rename]  -- EASY baseline
-- Pure column selection + rename + literal passthrough. Every arm
-- should nail this; it is the sanity floor.
-- ============================================================
CREATE OR REPLACE VIEW stg_products AS
SELECT
    sku            AS product_sku,
    prod_name      AS product_name,
    category       AS product_category,
    list_price     AS catalog_price
FROM raw_products;


-- ============================================================
-- stg_orders_unified  [T7 : no-RI union + inferred-key merge]  -- HARD
-- Unions two order sources with DIFFERENT schemas and NO shared key,
-- normalizing them into one shape. The seller join uses an inferred
-- relationship (seller_code) that is not a declared FK. Web orders
-- have no seller, so seller columns are NULL-filled for them.
-- The string date in the marketplace source is CAST here.
-- ============================================================
CREATE OR REPLACE TABLE stg_orders_unified AS
WITH web AS (
    SELECT
        TO_VARCHAR(order_id)          AS order_natural_key,
        'WEB'                         AS source_system,
        cust_email                    AS customer_ident,
        order_ts                      AS order_datetime,
        gross_amount                  AS order_amount,
        ship_country                  AS country,
        NULL                          AS seller_code
    FROM raw_orders_web
),
mkt AS (
    SELECT
        order_ref                     AS order_natural_key,
        'MKT'                         AS source_system,
        buyer_hash                    AS customer_ident,
        TO_TIMESTAMP_NTZ(placed_at)   AS order_datetime,
        amount_total                  AS order_amount,
        dest_country                  AS country,
        seller_code                   AS seller_code
    FROM raw_orders_mkt
),
unioned AS (
    SELECT * FROM web
    UNION ALL
    SELECT * FROM mkt
)
SELECT
    u.order_natural_key,
    u.source_system,
    u.customer_ident,
    u.order_datetime,
    u.order_amount,
    u.country,
    u.seller_code,
    s.seller_name,                    -- from inferred, non-FK join
    s.region                          AS seller_region
FROM unioned u
LEFT JOIN raw_sellers s
    ON u.seller_code = s.seller_code; -- inferred key, no constraint


-- ============================================================
-- stg_order_items_enriched  [T2 : multi-table join]  -- EASY-MED
-- Joins line items to the product catalog. Derives a line_total.
-- ============================================================
CREATE OR REPLACE VIEW stg_order_items_enriched AS
SELECT
    i.item_id,
    i.order_key,
    i.product_sku,
    p.product_name,
    p.product_category,
    i.qty,
    i.unit_price,
    i.qty * i.unit_price   AS line_total    -- derived from two cols
FROM raw_order_items i
LEFT JOIN stg_products p
    ON i.product_sku = p.product_sku;


-- ============================================================
-- stg_events_flat  [T-FLATTEN : lateral flatten of VARIANT]  -- HARD
-- Explodes nested JSON into relational columns. Static parsers must
-- trace flattened output columns back to the single VARIANT source
-- column (payload) and its JSON paths.
-- ============================================================
CREATE OR REPLACE VIEW stg_events_flat AS
SELECT
    e.event_id,
    e.payload:cust::VARCHAR            AS customer_ident,
    e.payload:type::VARCHAR            AS event_type,
    e.payload:props:sku::VARCHAR       AS product_sku,
    e.payload:props:dwell::NUMBER      AS dwell_seconds
FROM raw_events e;


-- ============================================================
-- stg_sales_base  [T3 : aggregate / GROUP BY]  -- MED
-- Aggregates enriched line items to order-grain totals. Introduces
-- INDIRECT lineage: grouping keys influence output rows but are not
-- the value source of the aggregated measures.
-- ============================================================
CREATE OR REPLACE TABLE stg_sales_base AS
SELECT
    ei.order_key,
    COUNT(*)                    AS item_count,
    SUM(ei.line_total)          AS order_line_revenue,
    AVG(ei.unit_price)          AS avg_unit_price,
    MAX(ei.product_category)    AS top_category
FROM stg_order_items_enriched ei
GROUP BY ei.order_key;
