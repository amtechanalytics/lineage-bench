-- ============================================================
-- DP LAYER : Archetype B -- calculation-heavy data products
-- Two data-product views where most output columns are EXPRESSIONS
-- over multiple source columns, not renames. Difficulty is DERIVED-
-- VALUE LINEAGE AT WIDTH: many-to-one edges explode, and the output
-- column names are business-y ("settlement_due", "margin_pct",
-- "make_whole") which bait the LLM into inferring the WRONG inputs
-- from the name rather than reading the expression.
--
-- Static parsers should recover the expression INPUTS accurately but
-- cannot express intent. The LLM may map a business-named output to
-- semantically-plausible-but-wrong source columns. That asymmetry is
-- the measurement target.
--
-- NOTE: all identifiers/domain/logic invented for this retail
-- benchmark; no resemblance to any real data product.
-- ============================================================

USE DATABASE OMNIMART_BENCH;
USE SCHEMA MART;

-- Physical source for the settlement/rebate-style calc view.
CREATE OR REPLACE TABLE ph_settlement_base (
    calc_run_key      VARCHAR,
    agrmnt_key        VARCHAR,
    agrmnt_nm         VARCHAR,
    partner_key       VARCHAR,
    partner_nm        VARCHAR,
    eval_start_dt     DATE,
    eval_end_dt       DATE,
    tier_cd           VARCHAR,
    rebate_rate       NUMBER(6,4),
    qual_purch_amt    NUMBER(14,2),
    qual_purch_qty    NUMBER,
    benefit_purch_amt NUMBER(14,2),
    benefit_purch_qty NUMBER,
    prev_settle_amt   NUMBER(14,2),
    list_cost_amt     NUMBER(14,2),
    net_cost_amt      NUMBER(14,2),
    dp_load_dt        DATE,
    dp_load_by        VARCHAR
);
INSERT INTO ph_settlement_base VALUES
    ('CR1','AG1','Spring Deal','PT1','Partner One','2024-01-01','2024-03-31',
     'T2',0.0500,100000.00,500,80000.00,400,3000.00,90000.00,72000.00,'2024-04-01','etl_user'),
    ('CR1','AG2','Volume Deal','PT2','Partner Two','2024-01-01','2024-03-31',
     'T1',0.0300,50000.00,250,50000.00,250,1000.00,45000.00,38000.00,'2024-04-01','etl_user');

-- Physical source for the order-economics calc view.
CREATE OR REPLACE TABLE ph_order_econ_base (
    order_key         VARCHAR,
    gross_amt         NUMBER(14,2),
    discount_amt      NUMBER(14,2),
    tax_amt           NUMBER(14,2),
    freight_amt       NUMBER(14,2),
    cogs_amt          NUMBER(14,2),
    return_amt        NUMBER(14,2),
    loyalty_pts_used  NUMBER,
    loyalty_pt_value  NUMBER(6,4),
    order_dt          DATE,
    cust_key          VARCHAR
);
INSERT INTO ph_order_econ_base VALUES
    ('O1',500.00,50.00,37.00,20.00,300.00,0.00,100,0.0100,'2024-01-15','C1'),
    ('O2',1200.00,120.00,89.00,35.00,700.00,150.00,0,0.0100,'2024-02-20','C1'),
    ('O3',300.00,0.00,24.00,15.00,180.00,0.00,50,0.0100,'2024-03-05','C2');


-- ============================================================
-- dp_settlement_summary  [DP-B1 : calc-heavy, rebate/settlement]
-- Renamed passthrough cols PLUS derived measures. Derived columns
-- and their true inputs:
--   rebate_amount      = qual_purch_amt * rebate_rate           (2 in)
--   margin_amount      = benefit_purch_amt - net_cost_amt       (2 in)
--   margin_pct         = (benefit_purch_amt - net_cost_amt)
--                          / NULLIF(benefit_purch_amt,0)        (2 in)
--   payment_due_amount = (qual_purch_amt*rebate_rate)
--                          - prev_settle_amt                    (3 in)
--   make_whole_amount  = CASE tier arithmetic over benefit/list  (multi)
--   effective_rate     = rebate_amount / NULLIF(qual_purch_amt,0)(2 in)
-- ============================================================
CREATE OR REPLACE VIEW dp_settlement_summary AS
SELECT
    calc_run_key       AS calculation_run_key,
    agrmnt_key         AS agreement_key,
    agrmnt_nm          AS agreement_name,
    partner_key        AS partner_key,
    partner_nm         AS partner_name,
    eval_start_dt      AS evaluation_start_date,
    eval_end_dt        AS evaluation_end_date,
    tier_cd            AS tier_code,
    rebate_rate        AS rebate_rate,
    qual_purch_amt     AS qualifying_purchase_amount,
    benefit_purch_amt  AS benefit_purchase_amount,
    prev_settle_amt    AS previous_settlement_amount,
    -- derived measures
    qual_purch_amt * rebate_rate                          AS rebate_amount,
    benefit_purch_amt - net_cost_amt                      AS margin_amount,
    (benefit_purch_amt - net_cost_amt)
        / NULLIF(benefit_purch_amt, 0)                    AS margin_pct,
    (qual_purch_amt * rebate_rate) - prev_settle_amt      AS payment_due_amount,
    CASE
        WHEN tier_cd = 'T1' THEN (benefit_purch_amt - list_cost_amt) * 0.10
        WHEN tier_cd = 'T2' THEN (benefit_purch_amt - list_cost_amt) * 0.20
        ELSE 0
    END                                                   AS make_whole_amount,
    (qual_purch_amt * rebate_rate)
        / NULLIF(qual_purch_amt, 0)                       AS effective_rate,
    DATEDIFF('day', eval_start_dt, eval_end_dt)           AS evaluation_days,
    dp_load_dt         AS dataproduct_load_date,
    dp_load_by         AS dataproduct_load_by
FROM ph_settlement_base;


-- ============================================================
-- dp_order_economics  [DP-B2 : calc-heavy, order P&L + window]
-- Derived columns and true inputs:
--   net_revenue       = gross_amt - discount_amt - return_amt   (3 in)
--   gross_margin_amt  = (gross_amt - discount_amt) - cogs_amt   (3 in)
--   gross_margin_pct  = ((gross_amt-discount_amt)-cogs_amt)
--                         / NULLIF(gross_amt-discount_amt,0)     (3 in)
--   loyalty_burn_amt  = loyalty_pts_used * loyalty_pt_value      (2 in)
--   total_landed_amt  = gross_amt + tax_amt + freight_amt        (3 in)
--   running_cust_rev  = SUM(net) OVER (PARTITION cust ORDER dt)  (multi + INDIRECT)
--   rev_rank_in_cust  = ROW_NUMBER() OVER (PARTITION cust ORDER net DESC) (INDIRECT)
-- ============================================================
CREATE OR REPLACE VIEW dp_order_economics AS
SELECT
    order_key          AS order_key,
    cust_key           AS customer_key,
    order_dt           AS order_date,
    gross_amt          AS gross_amount,
    discount_amt       AS discount_amount,
    tax_amt            AS tax_amount,
    freight_amt        AS freight_amount,
    cogs_amt           AS cost_of_goods_amount,
    -- derived measures
    gross_amt - discount_amt - return_amt                 AS net_revenue,
    (gross_amt - discount_amt) - cogs_amt                 AS gross_margin_amount,
    ((gross_amt - discount_amt) - cogs_amt)
        / NULLIF(gross_amt - discount_amt, 0)             AS gross_margin_pct,
    loyalty_pts_used * loyalty_pt_value                    AS loyalty_burn_amount,
    gross_amt + tax_amt + freight_amt                     AS total_landed_amount,
    SUM(gross_amt - discount_amt - return_amt) OVER (
        PARTITION BY cust_key ORDER BY order_dt
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )                                                     AS running_customer_revenue,
    ROW_NUMBER() OVER (
        PARTITION BY cust_key
        ORDER BY (gross_amt - discount_amt - return_amt) DESC
    )                                                     AS revenue_rank_in_customer
FROM ph_order_econ_base;
