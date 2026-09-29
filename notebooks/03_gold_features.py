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
# MAGIC `TARGET_MONTH` is the check-in month we price for. Default **7 (July)** — the peak — so the demo
# MAGIC shows dramatic (but capped) uplift. Change it to see prices soften in the off-season.
# MAGIC The weights/caps below are the *entire* pricing policy — all in one place, all explainable.

# COMMAND ----------
SOURCE = "samples.wanderbricks"
CAT, SCH = "hackathon", "data_axle"
spark.sql(f"USE CATALOG {CAT}"); spark.sql(f"USE SCHEMA {SCH}")

TARGET_MONTH = 7          # check-in month we price for (1-12). 7 = July (peak) for demo punch.

# --- pricing policy (fractions of base price; all capped so prices stay sane) ---
SEASON_MIN, SEASON_MAX = -0.20, 0.60   # season can swing price -20%..+60%
COMP_TOWARD_MEDIAN     = 0.30          # if under-priced vs destination, close 30% of the gap...
COMP_CAP               = 0.15          # ...but never more than +15% of base from comps
FX_WEIGHT              = 0.50          # capture half of a currency's weakening as uplift
FX_PCT_CAP             = 10.0          # ignore FX moves beyond ±10% (outliers)
HOLIDAY_UPLIFT         = 0.08          # +8% if a public holiday falls in the target month
PRICE_FLOOR, PRICE_CEIL = 0.75, 1.90   # final suggested price clamped to 0.75x..1.9x base

print(f"Pricing for check-in month = {TARGET_MONTH}")

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
# MAGIC ## 2. Seasonality — destination × month demand index
# MAGIC For each destination, how busy is each month vs. that destination's *own* average month?
# MAGIC `season_index = bookings_in_month / avg_bookings_per_month`. So 3.0 means "3× a normal month."
# MAGIC We read the index for `TARGET_MONTH` per destination. **This is the core of the thesis.**

# COMMAND ----------
spark.sql(f"""
CREATE OR REPLACE TEMP VIEW v_dest_season AS
WITH by_month AS (
  SELECT p.destination_id, month(b.check_in) AS mth, count(*) AS bookings
  FROM {SOURCE}.bookings b
  JOIN {SOURCE}.properties p ON b.property_id = p.property_id
  GROUP BY p.destination_id, month(b.check_in)
),
dest_avg AS (
  SELECT destination_id, avg(bookings) AS avg_month_bookings
  FROM by_month GROUP BY destination_id
)
SELECT m.destination_id,
       m.bookings AS target_month_bookings,
       round(a.avg_month_bookings, 1) AS avg_month_bookings,
       round(m.bookings / a.avg_month_bookings, 2) AS season_index
FROM by_month m
JOIN dest_avg a ON m.destination_id = a.destination_id
WHERE m.mth = {TARGET_MONTH}
""")
display(spark.sql("SELECT * FROM v_dest_season ORDER BY season_index DESC LIMIT 10"))

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
WITH holiday_flag AS (   -- does a public holiday fall in TARGET_MONTH for the destination's country?
  SELECT DISTINCT dc.country
  FROM holidays h JOIN dim_country dc ON dc.country = h.country
  WHERE month(to_date(h.holiday_date)) = {TARGET_MONTH}
),
base AS (
  SELECT
    p.property_id, p.title, p.base_price, p.property_type,
    p.destination_id, d.destination, d.country,
    p.property_latitude AS lat, p.property_longitude AS lon,
    dc.currency,
    coalesce(s.season_index, 1.0)                 AS season_index,
    coalesce(dp.dest_median_price, p.base_price)  AS dest_median_price,
    coalesce(pop.popularity_pct, 0.0)             AS popularity_pct,
    coalesce(occ.occupancy_pct, 0.0)              AS occupancy_pct,
    coalesce(fx.pct_change_yoy, 0.0)              AS fx_pct_change,
    CASE WHEN hf.country IS NOT NULL THEN true ELSE false END AS holiday_in_month
  FROM {SOURCE}.properties p
  JOIN {SOURCE}.destinations d ON p.destination_id = d.destination_id
  LEFT JOIN dim_country dc     ON dc.country = d.country
  LEFT JOIN v_dest_season s    ON s.destination_id = p.destination_id
  LEFT JOIN v_dest_price dp    ON dp.destination_id = p.destination_id
  LEFT JOIN v_prop_pop pop     ON pop.property_id = p.property_id
  LEFT JOIN v_prop_occ occ     ON occ.property_id = p.property_id
  LEFT JOIN fx_rates fx        ON fx.currency = dc.currency
  LEFT JOIN holiday_flag hf    ON hf.country = d.country
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
    -- HOLIDAY: flat % if a holiday lands in the target month
    round(CASE WHEN holiday_in_month THEN base_price * {HOLIDAY_UPLIFT} ELSE 0 END, 2) AS holiday_uplift
  FROM base
)
SELECT
  property_id, title, property_type, destination_id, destination, country, currency, lat, lon,
  base_price,
  season_index, dest_median_price, popularity_pct, occupancy_pct, fx_pct_change, holiday_in_month,
  season_uplift, comp_uplift, fx_uplift, holiday_uplift,
  -- suggested price = base + all uplifts, clamped to [FLOOR, CEIL] x base
  round(
    greatest(base_price * {PRICE_FLOOR},
      least(base_price * {PRICE_CEIL},
            base_price + season_uplift + comp_uplift + fx_uplift + holiday_uplift)), 2
  ) AS suggested_price,
  {TARGET_MONTH} AS target_month
FROM uplifts
""")
print("gold_property_features rows:", spark.table("gold_property_features").count())

# COMMAND ----------
# MAGIC %md
# MAGIC ### Sanity-check the waterfall on real listings
# MAGIC Look at a few Phuket properties: each term should read like the mock, and `suggested_price`
# MAGIC should equal `base + the four uplifts` (unless clamped).

# COMMAND ----------
display(spark.sql("""
SELECT title, base_price, season_uplift, comp_uplift, fx_uplift, holiday_uplift,
       suggested_price,
       round(100*(suggested_price-base_price)/base_price,1) AS uplift_pct
FROM gold_property_features
WHERE destination = 'Phuket'
ORDER BY uplift_pct DESC LIMIT 8
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ### How much revenue upside across the marketplace?
# MAGIC The one-number business case for the target month.

# COMMAND ----------
display(spark.sql("""
SELECT count(*) AS listings,
       round(avg(suggested_price - base_price), 2) AS avg_uplift_per_night,
       round(sum(suggested_price - base_price)) AS total_uplift_per_night_all_listings,
       round(100*avg((suggested_price-base_price)/base_price),1) AS avg_uplift_pct
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
# MAGIC   signals, currency, holiday flag, lat/lon. One row per property, priced for `TARGET_MONTH`.
# MAGIC - **`gold_property_doc`** — `search_text` per property, ready to embed.
# MAGIC - **`dim_country`** — reusable country → currency/ISO map.
# MAGIC
# MAGIC You should be able to point at any listing and explain its price term by term. Try changing
# MAGIC `TARGET_MONTH` to 1 (January) and re-running §6 — watch the uplift shrink (off-season). That
# MAGIC contrast is a great demo moment.
# MAGIC
# MAGIC **Next:** `04_provision_lakebase.py` — stand up `pricewise-db` and verify the search extensions
# MAGIC (`lakebase_text`, `lakebase_vector`, PostGIS) — the critical unknown.
