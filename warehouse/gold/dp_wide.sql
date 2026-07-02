-- ============================================================
-- DP LAYER : Archetype A -- wide passthrough / rename data products
-- Two data-product views that rename a wide physical staging table
-- into business-friendly output columns. Difficulty here is WIDTH +
-- RENAME FIDELITY, not algorithmic complexity:
--   * many columns (60 / 90) -> one silent drop or misalign = error
--   * repeated column FAMILIES (buyer / shipto / payer / household)
--     where the same logical attribute recurs from the SAME source
--     row -> baits semantic cross-wiring (e.g. mapping payer_city
--     back to buyer_city because they are semantically identical)
--   * fully-qualified three-level source names
-- Renames keep the AS keyword and readable-but-abbreviated source
-- names. Lineage per column is 1:1 DIRECT; the challenge is getting
-- ALL of them right and not cross-wiring the families.
--
-- NOTE: identifiers, domain, namespace, and abbreviation scheme are
-- entirely invented for this retail benchmark.
-- ============================================================

USE DATABASE OMNIMART_BENCH;
USE SCHEMA MART;

-- Physical staging tables that the DP views read from. These are the
-- wide "silver-physical" sources with cryptic abbreviated columns.
-- (Seed rows kept minimal; only column structure matters for lineage.)

CREATE OR REPLACE TABLE ph_party_360 (
    -- buyer family
    byr_key           VARCHAR,
    byr_src_key       VARCHAR,
    byr_nm            VARCHAR,
    byr_email         VARCHAR,
    byr_phn           VARCHAR,
    byr_addr          VARCHAR,
    byr_city          VARCHAR,
    byr_state         VARCHAR,
    byr_postal        VARCHAR,
    byr_ctry          VARCHAR,
    byr_status        VARCHAR,
    byr_loyalty_tier  VARCHAR,
    byr_create_dt     DATE,
    -- shipto family (same logical attributes, different family)
    shp_key           VARCHAR,
    shp_src_key       VARCHAR,
    shp_nm            VARCHAR,
    shp_phn           VARCHAR,
    shp_addr          VARCHAR,
    shp_city          VARCHAR,
    shp_state         VARCHAR,
    shp_postal        VARCHAR,
    shp_ctry          VARCHAR,
    shp_residential_flg VARCHAR,
    -- payer family
    pyr_key           VARCHAR,
    pyr_src_key       VARCHAR,
    pyr_nm            VARCHAR,
    pyr_addr          VARCHAR,
    pyr_city          VARCHAR,
    pyr_state         VARCHAR,
    pyr_postal        VARCHAR,
    pyr_ctry          VARCHAR,
    pyr_credit_hold_flg VARCHAR,
    pyr_terms_cd      VARCHAR,
    -- household family
    hh_key            VARCHAR,
    hh_src_key        VARCHAR,
    hh_nm             VARCHAR,
    hh_size           NUMBER,
    hh_income_band    VARCHAR,
    hh_city           VARCHAR,
    hh_state          VARCHAR,
    hh_postal         VARCHAR,
    -- data product audit
    dp_load_dt        DATE,
    dp_load_by        VARCHAR
);
INSERT INTO ph_party_360 VALUES
    ('B1','BS1','Alice Buyer','a@x.com','555-1','1 A St','Austin','TX','78701','US','ACTIVE','GOLD','2023-01-01',
     'H1','HS1','Alice Ship','555-2','9 Ship Rd','Austin','TX','78701','US','N',
     'P1','PS1','Alice Pay','1 Pay Ave','Dallas','TX','75201','US','N','NET30',
     'HH1','HHS1','Buyer Household',3,'MID','Austin','TX','78701',
     '2024-01-01','etl_user');

CREATE OR REPLACE TABLE ph_item_catalog_wide (
    it_key            VARCHAR,
    it_src_key        VARCHAR,
    it_sku            VARCHAR,
    it_nm             VARCHAR,
    it_desc           VARCHAR,
    it_brand_fam      VARCHAR,
    it_brand_subfam   VARCHAR,
    it_brand_nm       VARCHAR,
    it_catg           VARCHAR,
    it_subcatg        VARCHAR,
    it_seg_cd         VARCHAR,
    it_seg_nm         VARCHAR,
    it_uom            VARCHAR,
    it_pack_size      NUMBER,
    it_list_price     NUMBER(12,2),
    it_status_cd      VARCHAR,
    it_status_desc    VARCHAR,
    it_status_grp     VARCHAR,
    it_status_dt      DATE,
    it_hazmat_flg     VARCHAR,
    it_perishable_flg VARCHAR,
    it_create_dt      DATE,
    dp_load_dt        DATE,
    dp_load_by        VARCHAR
);
INSERT INTO ph_item_catalog_wide VALUES
    ('I1','IS1','SKU-RED','Red Widget','A red widget','WidgetFam','SubW','RedBrand',
     'Widgets','Small','SEG1','Consumer','EA',1,60.00,'A','Active','Sellable','2023-01-01','N','N','2023-01-01',
     '2024-01-01','etl_user');


-- ============================================================
-- dp_party_360  [DP-A1 : wide rename, ~40 output cols, 4 families]
-- Business-friendly customer-360 data product. Every output column
-- is a 1:1 DIRECT rename of one ph_party_360 column. The buyer /
-- shipto / payer / household families repeat name/city/state/postal,
-- which is the cross-wiring bait.
-- ============================================================
CREATE OR REPLACE VIEW dp_party_360 AS
SELECT
    byr_key            AS buyer_key,
    byr_src_key        AS buyer_source_key,
    byr_nm             AS buyer_name,
    byr_email          AS buyer_email,
    byr_phn            AS buyer_phone,
    byr_addr           AS buyer_address,
    byr_city           AS buyer_city,
    byr_state          AS buyer_state,
    byr_postal         AS buyer_postal_code,
    byr_ctry           AS buyer_country,
    byr_status         AS buyer_status,
    byr_loyalty_tier   AS buyer_loyalty_tier,
    byr_create_dt      AS buyer_creation_date,
    shp_key            AS shipto_key,
    shp_src_key        AS shipto_source_key,
    shp_nm             AS shipto_name,
    shp_phn            AS shipto_phone,
    shp_addr           AS shipto_address,
    shp_city           AS shipto_city,
    shp_state          AS shipto_state,
    shp_postal         AS shipto_postal_code,
    shp_ctry           AS shipto_country,
    shp_residential_flg AS shipto_residential_flag,
    pyr_key            AS payer_key,
    pyr_src_key        AS payer_source_key,
    pyr_nm             AS payer_name,
    pyr_addr           AS payer_address,
    pyr_city           AS payer_city,
    pyr_state          AS payer_state,
    pyr_postal         AS payer_postal_code,
    pyr_ctry           AS payer_country,
    pyr_credit_hold_flg AS payer_credit_hold_flag,
    pyr_terms_cd       AS payer_payment_terms_code,
    hh_key             AS household_key,
    hh_src_key         AS household_source_key,
    hh_nm              AS household_name,
    hh_size            AS household_size,
    hh_income_band     AS household_income_band,
    hh_city            AS household_city,
    hh_state           AS household_state,
    hh_postal          AS household_postal_code,
    dp_load_dt         AS dataproduct_load_date,
    dp_load_by         AS dataproduct_load_by
FROM ph_party_360;


-- ============================================================
-- dp_item_catalog  [DP-A2 : wide rename, ~23 output cols]
-- Narrower wide-view; single family, still all 1:1 DIRECT renames.
-- Acts as the mid-point on the width gradient.
-- ============================================================
CREATE OR REPLACE VIEW dp_item_catalog AS
SELECT
    it_key             AS item_key,
    it_src_key         AS item_source_key,
    it_sku             AS item_sku,
    it_nm              AS item_name,
    it_desc            AS item_description,
    it_brand_fam       AS item_brand_family,
    it_brand_subfam    AS item_brand_subfamily,
    it_brand_nm        AS item_brand_name,
    it_catg            AS item_category,
    it_subcatg         AS item_subcategory,
    it_seg_cd          AS item_segment_code,
    it_seg_nm          AS item_segment_name,
    it_uom             AS item_unit_of_measure,
    it_pack_size       AS item_pack_size,
    it_list_price      AS item_list_price,
    it_status_cd       AS item_status_code,
    it_status_desc     AS item_status_description,
    it_status_grp      AS item_status_group,
    it_status_dt       AS item_status_start_date,
    it_hazmat_flg      AS item_hazmat_flag,
    it_perishable_flg  AS item_perishable_flag,
    it_create_dt       AS item_creation_date,
    dp_load_dt         AS dataproduct_load_date,
    dp_load_by         AS dataproduct_load_by
FROM ph_item_catalog_wide;
