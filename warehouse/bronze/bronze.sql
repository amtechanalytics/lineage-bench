-- ============================================================
-- BRONZE LAYER : raw landing zone
-- Deliberately NO primary keys, NO foreign keys, inconsistent id
-- conventions across the two order sources. This is realistic for
-- a marketplace that lands data from a first-party storefront and
-- from third-party sellers, and it is what creates the T7 no-RI
-- merge problem downstream.
--
-- These are source tables: in the gold edge file they are ORIGINS
-- (no upstream lineage). Seed rows are tiny by design -- the row
-- COUNT is irrelevant to lineage; only column structure matters.
-- ============================================================

USE DATABASE OMNIMART_BENCH;
USE SCHEMA MART;

-- ---- raw_orders_web : first-party storefront orders --------------
-- id convention: numeric order_id, customer as email string
CREATE OR REPLACE TABLE raw_orders_web (
    order_id       NUMBER,
    cust_email     VARCHAR,
    order_ts       TIMESTAMP_NTZ,
    gross_amount   NUMBER(12,2),
    ship_country   VARCHAR
);
INSERT INTO raw_orders_web VALUES
    (1001, 'a@x.com',  '2024-01-05 10:00:00', 120.00, 'US'),
    (1002, 'b@x.com',  '2024-02-11 12:30:00',  80.50, 'US'),
    (1003, 'c@x.com',  '2024-02-20 09:15:00', 200.00, 'CA'),
    (1004, 'a@x.com',  '2024-03-02 14:45:00',  55.00, 'US'),
    (1005, 'd@x.com',  '2024-03-18 16:20:00', 320.75, 'GB');

-- ---- raw_orders_mkt : third-party marketplace orders -------------
-- DIFFERENT id convention: string order ref, customer as hashed id,
-- seller-scoped. No shared key with raw_orders_web. This is the
-- no-referential-integrity reality the merge must reconcile.
CREATE OR REPLACE TABLE raw_orders_mkt (
    order_ref      VARCHAR,        -- e.g. 'MKT-88231'
    buyer_hash     VARCHAR,        -- opaque, not an email
    seller_code    VARCHAR,
    placed_at      VARCHAR,        -- string date, not a timestamp type
    amount_total   NUMBER(12,2),
    dest_country   VARCHAR
);
INSERT INTO raw_orders_mkt VALUES
    ('MKT-88231', 'h1a2', 'S01', '2024-01-22', 149.99, 'US'),
    ('MKT-88232', 'h9z8', 'S02', '2024-02-14',  42.00, 'US'),
    ('MKT-88233', 'h1a2', 'S01', '2024-03-09', 610.00, 'DE'),
    ('MKT-88234', 'h4k5', 'S03', '2024-03-25',  27.49, 'US');

-- ---- raw_order_items : line items (source-agnostic) --------------
-- order_key here is a soft reference: it may hold EITHER a web
-- order_id (as string) OR a marketplace order_ref. Nothing enforces it.
CREATE OR REPLACE TABLE raw_order_items (
    item_id        NUMBER,
    order_key      VARCHAR,        -- soft link to either order source
    product_sku    VARCHAR,
    qty            NUMBER,
    unit_price     NUMBER(12,2)
);
INSERT INTO raw_order_items VALUES
    (1, '1001', 'SKU-RED',  2, 60.00),
    (2, '1002', 'SKU-BLU',  1, 80.50),
    (3, '1003', 'SKU-RED',  1, 200.00),
    (4, 'MKT-88231', 'SKU-GRN', 3, 49.99),
    (5, 'MKT-88233', 'SKU-BLU', 5, 122.00),
    (6, '1005', 'SKU-GRN',  4, 80.18);

-- ---- raw_products : catalog --------------------------------------
CREATE OR REPLACE TABLE raw_products (
    sku            VARCHAR,
    prod_name      VARCHAR,
    category       VARCHAR,
    list_price     NUMBER(12,2)
);
INSERT INTO raw_products VALUES
    ('SKU-RED', 'Red Widget',   'Widgets',   60.00),
    ('SKU-BLU', 'Blue Gadget',  'Gadgets',   80.50),
    ('SKU-GRN', 'Green Gizmo',  'Gizmos',    49.99);

-- ---- raw_sellers : third-party seller registry -------------------
CREATE OR REPLACE TABLE raw_sellers (
    seller_code    VARCHAR,
    seller_name    VARCHAR,
    region         VARCHAR
);
INSERT INTO raw_sellers VALUES
    ('S01', 'Acme Resellers',  'NA'),
    ('S02', 'Bolt Trading',    'NA'),
    ('S03', 'Cobalt Import',   'EU');

-- ---- raw_events : semi-structured clickstream (JSON VARIANT) -----
-- Sets up the LATERAL FLATTEN case: a single VARIANT column whose
-- nested fields become relational columns downstream.
CREATE OR REPLACE TABLE raw_events (
    event_id       NUMBER,
    payload        VARIANT
);
INSERT INTO raw_events
    SELECT 1, PARSE_JSON('{"cust":"a@x.com","type":"view","props":{"sku":"SKU-RED","dwell":12}}') UNION ALL
    SELECT 2, PARSE_JSON('{"cust":"b@x.com","type":"add_cart","props":{"sku":"SKU-BLU","dwell":5}}') UNION ALL
    SELECT 3, PARSE_JSON('{"cust":"a@x.com","type":"purchase","props":{"sku":"SKU-RED","dwell":30}}');
