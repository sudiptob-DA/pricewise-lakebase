# Databricks notebook source
# MAGIC %md
# MAGIC # PriceWise · 03 · Gold features
# MAGIC
# MAGIC **Goal:** turn raw `wanderbricks` + `fx_rates` + `holidays` into the two gold tables that power
# MAGIC the app:
# MAGIC - **`gold_property_doc`** — the text we embed for semantic search (title + description + destination + amenities).
# MAGIC - **`gold_property_features`** — the pricing + demand signals, incl. the **explainable price waterfall**.
# MAGIC
# MAGIC **Design principles (why it looks like this):**
# MAGIC 1. **Explainable, not a black box.** `suggested_price` is an additive waterfall —
# MAGIC    `base → +season → +comps → +FX → +holiday` — each term stored separately so the UI (and a judge)
# MAGIC    can see exactly why the price moved.
# MAGIC 2. **Robust where data is thin.** Bookings are ~4/property, so seasonality is computed at the
# MAGIC    **destination × month** level (lots of data), while popularity comes from **clickstream** (per property).
# MAGIC 3. **Transparent heuristic, swappable later.** Every weight is a named constant you can tune. A trained
# MAGIC    model could replace this without changing the schema.
# MAGIC
# MAGIC > Import to workspace, attach Serverless, Run All. Requires notebooks 01–02 to have run
# MAGIC > (`fx_rates`, `holidays` must exist in `hackathon.data_axle`).

# COMMAND ----------
# MAGIC %md
# MAGIC ## 0. Setup + tunable knobs
# MAGIC We price for **all 12 months** — one row per `(property_id, month)` — so the app can price any
# MAGIC requested check-in date, and so we can show the same villa costing different amounts across the
# MAGIC year (the real proof of dynamic pricing). `DEMO_MONTH` is only used to *filter the sanity checks*
# MAGIC below (July = peak, the punchiest view); it does NOT limit what we compute.
# MAGIC The weights/caps below are the *entire* pricing policy — all in one place, all explainable.

# COMMAND ----------
SOURCE = "samples.wanderbricks"
CAT, SCH = "hackathon", "data_axle"
spark.sql(f"USE CATALOG {CAT}"); spark.sql(f"USE SCHEMA {SCH}")

DEMO_MONTH = 7            # ONLY for eyeballing results below (7 = July peak). Not a compute limit.

# --- pricing policy (fractions of base price; all capped so prices stay sane) ---
SEASON_MIN, SEASON_MAX = -0.20, 0.60   # season can swing price -20%..+60%
COMP_TOWARD_MEDIAN     = 0.30          # if under-priced vs destination, close 30% of the gap...
COMP_CAP               = 0.15          # ...but never more than +15% of base from comps
FX_WEIGHT              = 0.50          # capture half of a currency's weakening as uplift
FX_PCT_CAP             = 10.0          # ignore FX moves beyond ±10% (outliers)
HOLIDAY_UPLIFT         = 0.08          # +8% if a public holiday falls in that month
PRICE_FLOOR, PRICE_CEIL = 0.75, 1.90   # final suggested price clamped to 0.75x..1.9x base

print(f"Pricing ALL 12 months. Sanity checks below filter to DEMO_MONTH = {DEMO_MONTH}")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 1. `dim_country` — clean country → currency / ISO mapping
# MAGIC The join key that ties destinations to FX (currency) and holidays (country). We persist it so
# MAGIC every downstream join is a simple, readable equality — no giant CASE statements.

# COMMAND ----------
from pyspark.sql import Row
COUNTRY_MAP = {   # country name (as in destinations) -> (ISO2, currency)
    "Thailand": ("TH", "THB"), "Spain": ("ES", "EUR"), "Australia": ("AU", "AUD"),
    "France": ("FR", "EUR"), "United Arab Emirates": ("AE", "AED"), "Japan": ("JP", "JPY"),
    "Singapore": ("SG", "SGD"), "United States": ("US", "USD"), "Germany": ("DE", "EUR"),
    "Italy": ("IT", "EUR"), "United Kingdom": ("GB", "GBP"), "Egypt": ("EG", "EGP"),
    "India": ("IN", "INR"), "Greece": ("GR", "EUR"), "Canada": ("CA", "CAD"),
    "Switzerland": ("CH", "CHF"), "Austria": ("AT", "EUR"), "China": ("CN", "CNY"),
}
spark.createDataFrame([Row(country=k, iso2=v[0], currency=v[1]) for k, v in COUNTRY_MAP.items()]) \
     .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable("dim_country")
display(spark.table("dim_country"))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 2. Seasonality — destination × month demand index (ALL months)
# MAGIC For each destination *and each month*, how busy is it vs. that destination's *own* average month?
# MAGIC `season_index = bookings_in_month / avg_bookings_per_month`. So 3.0 means "3× a normal month."
# MAGIC We keep all 12 months so every property can be priced for any check-in month. To guarantee a
# MAGIC complete grid (some destination/month combos may have zero bookings), we cross-join
# MAGIC destinations × months 1–12 and treat missing months as 0 bookings. **This is the core of the thesis.**

# COMMAND ----------
spark.sql(f"""
CREATE OR REPLACE TEMP VIEW v_dest_season AS
WITH months AS (SELECT explode(sequence(1, 12)) AS mth),
dests AS (SELECT DISTINCT destination_id FROM {SOURCE}.properties),
grid AS (SELECT d.destination_id, m.mth FROM dests d CROSS JOIN months m),
by_month AS (
  SELECT p.destination_id, month(b.check_in) AS mth, count(*) AS bookings
  FROM {SOURCE}.bookings b
  JOIN {SOURCE}.properties p ON b.property_id = p.property_id
  GROUP BY p.destination_id, month(b.check_in)
),
filled AS (
  SELECT g.destination_id, g.mth, coalesce(bm.bookings, 0) AS bookings
  FROM grid g LEFT JOIN by_month bm
    ON bm.destination_id = g.destination_id AND bm.mth = g.mth
),
dest_avg AS (
  SELECT destination_id, avg(bookings) AS avg_month_bookings
  FROM filled GROUP BY destination_id
)
SELECT f.destination_id, f.mth,
       f.bookings AS month_bookings,
       round(a.avg_month_bookings, 1) AS avg_month_bookings,
       round(f.bookings / nullif(a.avg_month_bookings, 0), 2) AS season_index
FROM filled f
JOIN dest_avg a ON f.destination_id = a.destination_id
""")
display(spark.sql(f"SELECT * FROM v_dest_season WHERE mth = {DEMO_MONTH} ORDER BY season_index DESC LIMIT 10"))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 3. Popularity — per-property demand from clickstream
# MAGIC Clickstream is a one-day snapshot, so we use it for **relative** popularity (a percentile rank),
# MAGIC not a trend. This feeds search ranking and the "demand" gauge — **not** the price waterfall
# MAGIC (season already captures time-based demand; we don't want to double-count).

# COMMAND ----------
spark.sql(f"""
CREATE OR REPLACE TEMP VIEW v_prop_pop AS
WITH ev AS (
  SELECT property_id, count(*) AS events
  FROM {SOURCE}.clickstream GROUP BY property_id
)
SELECT property_id, events,
       round(percent_rank() OVER (ORDER BY events), 3) AS popularity_pct  -- 0..1
FROM ev
""")
display(spark.sql("SELECT * FROM v_prop_pop ORDER BY events DESC LIMIT 5"))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 4. Comp baseline — price percentile within destination
# MAGIC A simple, honest comp signal: where does this listing sit vs. peers in the same destination?
# MAGIC If it's *below* the destination median, there's room to raise. (The richer **semantic 20-mi comp**
# MAGIC is an enhancement done in Lakebase — see `ENHANCEMENTS.md` E2. This destination-level baseline is
# MAGIC what feeds the initial `suggested_price`.)

# COMMAND ----------
spark.sql(f"""
CREATE OR REPLACE TEMP VIEW v_dest_price AS
SELECT destination_id,
       percentile_approx(base_price, 0.5) AS dest_median_price
FROM {SOURCE}.properties GROUP BY destination_id
""")
display(spark.sql("SELECT * FROM v_dest_price ORDER BY dest_median_price DESC LIMIT 8"))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 5. Occupancy proxy (stored for display / analytics)
# MAGIC Bookings are thin per property, so this is a percentile of booked volume — a relative "how booked
# MAGIC is this listing" signal. Stored for the Pricing Studio and Genie, not used in the price waterfall.

# COMMAND ----------
spark.sql(f"""
CREATE OR REPLACE TEMP VIEW v_prop_occ AS
WITH bk AS (
  SELECT property_id, count(*) AS bookings
  FROM {SOURCE}.bookings
  WHERE status IN ('confirmed','completed')
  GROUP BY property_id
)
SELECT property_id, bookings,
       round(percent_rank() OVER (ORDER BY bookings), 3) AS occupancy_pct
FROM bk
""")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 6. ⭐ Assemble `gold_property_features` — the explainable price waterfall
# MAGIC We bring every signal together and compute each uplift term **separately**, then sum + clamp.
# MAGIC Read the CASE/round expressions like the waterfall in the mock:
# MAGIC `base → +season_uplift → +comp_uplift → +fx_uplift → +holiday_uplift → suggested_price`.

# COMMAND ----------
spark.sql(f"""
CREATE OR REPLACE TABLE gold_property_features AS
WITH holiday_by_month AS (   -- which (country, month) combos have a public holiday?
  SELECT DISTINCT dc.country, month(to_date(h.holiday_date)) AS mth
  FROM holidays h JOIN dim_country dc ON dc.country = h.country
),
base AS (
  SELECT
    p.property_id, p.title, p.base_price, p.property_type,
    p.destination_id, d.destination, d.country,
    p.property_latitude AS lat, p.property_longitude AS lon,
    dc.currency,
    s.mth,                                        -- <-- the month we're pricing for (1..12)
    coalesce(s.season_index, 1.0)                 AS season_index,
    coalesce(dp.dest_median_price, p.base_price)  AS dest_median_price,
    coalesce(pop.popularity_pct, 0.0)             AS popularity_pct,
    coalesce(occ.occupancy_pct, 0.0)              AS occupancy_pct,
    coalesce(fx.pct_change_yoy, 0.0)              AS fx_pct_change,
    CASE WHEN hm.country IS NOT NULL THEN true ELSE false END AS holiday_in_month
  FROM {SOURCE}.properties p
  JOIN {SOURCE}.destinations d ON p.destination_id = d.destination_id
  LEFT JOIN dim_country dc     ON dc.country = d.country
  JOIN v_dest_season s         ON s.destination_id = p.destination_id     -- 12 rows per property
  LEFT JOIN v_dest_price dp    ON dp.destination_id = p.destination_id
  LEFT JOIN v_prop_pop pop     ON pop.property_id = p.property_id
  LEFT JOIN v_prop_occ occ     ON occ.property_id = p.property_id
  LEFT JOIN fx_rates fx        ON fx.currency = dc.currency
  LEFT JOIN holiday_by_month hm ON hm.country = d.country AND hm.mth = s.mth
),
uplifts AS (
  SELECT *,
    -- SEASON: (index-1) clamped to [SEASON_MIN, SEASON_MAX], applied to base
    round(base_price * greatest({SEASON_MIN}, least({SEASON_MAX}, season_index - 1.0)), 2) AS season_uplift,
    -- COMP: if under destination median, close COMP_TOWARD_MEDIAN of the gap, capped at COMP_CAP*base
    round(CASE WHEN base_price < dest_median_price
               THEN least((dest_median_price - base_price) * {COMP_TOWARD_MEDIAN}, base_price * {COMP_CAP})
               ELSE 0 END, 2) AS comp_uplift,
    -- FX: only a weakening destination currency (positive pct) gives uplift, capped, weighted
    round(base_price * greatest(0.0, least({FX_PCT_CAP}, fx_pct_change)) / 100.0 * {FX_WEIGHT}, 2) AS fx_uplift,
    -- HOLIDAY: flat % if a holiday lands in that month
    round(CASE WHEN holiday_in_month THEN base_price * {HOLIDAY_UPLIFT} ELSE 0 END, 2) AS holiday_uplift
  FROM base
)
SELECT
  property_id, mth AS target_month,
  title, property_type, destination_id, destination, country, currency, lat, lon,
  base_price,
  season_index, dest_median_price, popularity_pct, occupancy_pct, fx_pct_change, holiday_in_month,
  season_uplift, comp_uplift, fx_uplift, holiday_uplift,
  -- suggested price = base + all uplifts, clamped to [FLOOR, CEIL] x base
  round(
    greatest(base_price * {PRICE_FLOOR},
      least(base_price * {PRICE_CEIL},
            base_price + season_uplift + comp_uplift + fx_uplift + holiday_uplift)), 2
  ) AS suggested_price
FROM uplifts
""")
_n = spark.table("gold_property_features").count()
print(f"gold_property_features rows: {_n:,}  (~18k properties x 12 months)")

# COMMAND ----------
# MAGIC %md
# MAGIC ### Sanity-check the waterfall on real listings (DEMO_MONTH)
# MAGIC Look at a few Phuket properties for the peak month: each term should read like the mock, and
# MAGIC `suggested_price` should equal `base + the four uplifts` (unless clamped).

# COMMAND ----------
display(spark.sql(f"""
SELECT title, base_price, season_uplift, comp_uplift, fx_uplift, holiday_uplift,
       suggested_price,
       round(100*(suggested_price-base_price)/base_price,1) AS uplift_pct
FROM gold_property_features
WHERE destination = 'Phuket' AND target_month = {DEMO_MONTH}
ORDER BY uplift_pct DESC LIMIT 8
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ### ⭐ The dynamic-pricing proof — ONE villa across all 12 months
# MAGIC This is the demo beat that hardcoding a single month would have hidden: the *same* listing
# MAGIC priced month by month. Watch `suggested_price` rise into the peak and soften off-season.

# COMMAND ----------
display(spark.sql("""
WITH one AS (   -- pick a single Phuket property that has real seasonal swing
  SELECT property_id FROM gold_property_features
  WHERE destination='Phuket'
  GROUP BY property_id
  ORDER BY max(suggested_price) - min(suggested_price) DESC LIMIT 1
)
SELECT f.target_month, f.base_price, f.season_index,
       f.season_uplift, f.comp_uplift, f.fx_uplift, f.holiday_uplift, f.suggested_price
FROM gold_property_features f JOIN one ON one.property_id = f.property_id
ORDER BY f.target_month
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC Chart tip: on the result above, Visualization → Line, X = `target_month`, Y = `suggested_price`.
# MAGIC That curve (cheap in Jan, peak in Jul) is the dynamic-pricing screenshot for the demo.

# COMMAND ----------
# MAGIC %md
# MAGIC ### How much revenue upside across the marketplace?
# MAGIC The one-number business case. Per month, and averaged across the whole year.

# COMMAND ----------
display(spark.sql(f"""
SELECT target_month,
       count(*) AS listings,
       round(avg(suggested_price - base_price), 2) AS avg_uplift_per_night,
       round(100*avg((suggested_price-base_price)/base_price),1) AS avg_uplift_pct
FROM gold_property_features
GROUP BY target_month ORDER BY target_month
"""))

# COMMAND ----------
display(spark.sql(f"""
SELECT round(avg(suggested_price - base_price), 2) AS avg_uplift_per_night_all_year,
       round(100*avg((suggested_price-base_price)/base_price),1) AS avg_uplift_pct_all_year,
       (SELECT round(avg(suggested_price-base_price),2) FROM gold_property_features WHERE target_month={DEMO_MONTH}) AS avg_uplift_peak_month
FROM gold_property_features
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 7. `gold_property_doc` — the text we embed (notebook 05)
# MAGIC Semantic search runs on **descriptions**, enriched with title, destination and amenities so a
# MAGIC query like "romantic sea-view escape with nightlife" matches on meaning. (Reviews are excluded —
# MAGIC remember notebook 01: they're templated.)

# COMMAND ----------
spark.sql(f"""
CREATE OR REPLACE TABLE gold_property_doc AS
WITH amen AS (
  SELECT pa.property_id, concat_ws(', ', collect_list(a.name)) AS amenities
  FROM {SOURCE}.property_amenities pa
  JOIN {SOURCE}.amenities a ON a.amenity_id = pa.amenity_id
  GROUP BY pa.property_id
)
SELECT
  p.property_id, p.title, p.description, p.property_type,
  d.destination, d.country, coalesce(am.amenities, '') AS amenities,
  -- search_text: what we feed the embedding model
  concat_ws('. ',
    p.title,
    p.description,
    concat('Located in ', d.destination, ', ', d.country),
    concat('Amenities: ', coalesce(am.amenities, 'n/a'))
  ) AS search_text
FROM {SOURCE}.properties p
JOIN {SOURCE}.destinations d ON p.destination_id = d.destination_id
LEFT JOIN amen am ON am.property_id = p.property_id
""")
display(spark.sql("SELECT property_id, title, substr(search_text,1,220) AS search_text_preview FROM gold_property_doc LIMIT 5"))

# COMMAND ----------
# MAGIC %md
# MAGIC ## ✅ Checkpoint
# MAGIC Two gold tables written to `hackathon.data_axle`:
# MAGIC - **`gold_property_features`** — base + 4 explainable uplifts → `suggested_price`, plus demand/occupancy
# MAGIC   signals, currency, holiday flag, lat/lon. **One row per (property, month)** — ~218k rows — so the
# MAGIC   app can price any check-in month. Serving key = `(property_id, target_month)`.
# MAGIC - **`gold_property_doc`** — `search_text` per property, ready to embed.
# MAGIC - **`dim_country`** — reusable country → currency/ISO map.
# MAGIC
# MAGIC You can now point at any listing and explain its price term by term, *and* show the same villa
# MAGIC priced across all 12 months (the dynamic-pricing proof — see the per-month line chart above).
# MAGIC
# MAGIC > Note: `suggested_price` here uses a **destination-level** comp baseline. The richer
# MAGIC > **semantic 20-mi comp** (pgvector + geo) refines it later in Lakebase — see `ENHANCEMENTS.md` E2.
# MAGIC
# MAGIC **Next:** `04_provision_lakebase.py` — stand up `pricewise-db` and verify the search extensions
# MAGIC (`lakebase_text`, `lakebase_vector`, PostGIS) — the critical unknown.
