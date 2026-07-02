-- ============================================================
-- OmniMart lineage benchmark : environment setup
-- Run this first in a Snowflake worksheet.
-- Creates an isolated database + schema. Nothing here is part of
-- the lineage evaluation itself; it only builds the substrate used
-- to execute-validate the gold edges.
-- ============================================================

CREATE DATABASE IF NOT EXISTS OMNIMART_BENCH;
USE DATABASE OMNIMART_BENCH;

CREATE SCHEMA IF NOT EXISTS MART;
USE SCHEMA MART;

-- All models below are created in OMNIMART_BENCH.MART.
-- The medallion "layers" (bronze/silver/gold) are a naming +
-- folder convention, not separate Snowflake schemas, to keep the
-- benchmark single-schema and the SQL directly runnable.
--
-- Run order:
--   1. 00_setup.sql
--   2. bronze/bronze.sql
--   3. silver/silver.sql
--   4. gold/gold.sql          (pivots, window, nested, CTE)
--   5. gold/dp_wide.sql       (DP archetype A: wide rename views)
--   6. gold/dp_calc.sql       (DP archetype B: calc-heavy views)
--   7. validate/validate.sql  (certify gold)
