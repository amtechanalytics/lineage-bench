-- ============================================================
-- RUN CERTIFICATION  (run these AFTER certify_gold_proc.sql is created)
-- ============================================================

USE DATABASE OMNIMART_BENCH;
USE SCHEMA MART;

-- ------------------------------------------------------------
-- A) Run it and see the flattened verdict table (one row per check).
--    Scan the VERDICT column; anything not PASS needs attention.
-- ------------------------------------------------------------
WITH r AS (SELECT certify_gold() AS j)
SELECT
    f.value:check::STRING     AS check_name,
    f.value:verdict::STRING   AS verdict,
    f.value:expected::STRING  AS expected,
    f.value:actual            AS actual
FROM r, TABLE(FLATTEN(input => r.j:checks)) f
ORDER BY (verdict='PASS'), check_name;   -- FAILs float to top


-- ------------------------------------------------------------
-- B) Get the OVERALL result + counts in one row.
-- ------------------------------------------------------------
WITH r AS (SELECT certify_gold() AS j)
SELECT
    r.j:overall::STRING       AS overall,
    r.j:passed::NUMBER        AS passed,
    r.j:total_checks::NUMBER  AS total_checks,
    r.j:certified_at::STRING  AS certified_at
FROM r;


-- ------------------------------------------------------------
-- C) THE ONE BLOB TO COPY.
--    Run this, click the single result cell, copy its full contents,
--    and paste it back. It contains every check + actual values as
--    one JSON object -- enough to shape into any table we need.
-- ------------------------------------------------------------
SELECT certify_gold()::STRING AS certification_blob;


-- ------------------------------------------------------------
-- D) Write the same JSON to the stage as a downloadable file.
-- ------------------------------------------------------------
COPY INTO @CERT_STAGE/certification.json
FROM (SELECT certify_gold())
FILE_FORMAT = (TYPE = JSON)
OVERWRITE = TRUE
SINGLE = TRUE;

-- Confirm it landed:
LIST @CERT_STAGE;

-- ------------------------------------------------------------
-- E) Download the file to your machine.
--    Run this from SnowSQL (the browser worksheet cannot GET files).
--    Adjust the local path as needed.
-- ------------------------------------------------------------
-- GET @CERT_STAGE/certification.json file:///tmp/;
