# Databricks notebook source
# MAGIC %md
# MAGIC # PriceWise · 01 · Explore the Lakehouse
# MAGIC
# MAGIC **Goal:** understand the raw data *before* we transform it. By the end you'll be able to
# MAGIC explain what every table gives us, and you'll have proven the core business insight with your
# MAGIC own eyes.
# MAGIC
# MAGIC **Source:** `samples.wanderbricks` — a fictional but rich global stay marketplace.
# MAGIC
# MAGIC > How to use: import this file into your Databricks workspace (Repos or Workspace import).
# MAGIC > Each `# COMMAND ----------` is a cell. Attach to Serverless, then Run All — or step through
# MAGIC > one cell at a time and read the markdown as you go.

# COMMAND ----------
# MAGIC %md
# MAGIC ## 0. Setup
# MAGIC We use plain Spark SQL against Unity Catalog. Nothing to install. `SOURCE` is the schema
# MAGIC we read from; `TARGET` is our team schema where later notebooks will write.

# COMMAND ----------
SOURCE = "samples.wanderbricks"
TARGET_CATALOG = "hackathon"
TARGET_SCHEMA = "data_axle"

spark.sql(f"USE CATALOG {TARGET_CATALOG}")
spark.sql(f"USE SCHEMA {TARGET_SCHEMA}")
print("Reading from:", SOURCE)
print("Our schema  :", f"{TARGET_CATALOG}.{TARGET_SCHEMA}")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 1. What tables exist?
# MAGIC A quick inventory. `wanderbricks` is relational — a marketplace spread across many tables,
# MAGIC not one flat file. That's what makes the joins (and our data-engineering story) real.

# COMMAND ----------
display(spark.sql(f"SHOW TABLES IN {SOURCE}"))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 2. Properties — the catalog we search & price
# MAGIC The heart of the app. Note the columns we'll lean on:
# MAGIC - `description` → **the text we embed** for semantic search (rich, unique).
# MAGIC - `base_price` → the anchor our suggested price adjusts from.
# MAGIC - `property_latitude` / `property_longitude` → geo (20-mi comps, weather-that-we-dropped).
# MAGIC - `destination_id` → joins to `destinations` (country, editorial guide).

# COMMAND ----------
display(spark.sql(f"SELECT * FROM {SOURCE}.properties LIMIT 10"))

# COMMAND ----------
display(spark.sql(f"""
SELECT count(*) AS n_props,
       count(DISTINCT destination_id) AS n_destinations,
       round(min(base_price))    AS min_price,
       round(avg(base_price))    AS avg_price,
       round(percentile_approx(base_price, 0.5)) AS median_price,
       round(max(base_price))    AS max_price
FROM {SOURCE}.properties
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ### Property types & top destinations
# MAGIC Tells us the marketplace is **beach + city dominated** (Summer Getaway / Urban), with only a
# MAGIC handful of ski stays. This is *why* we build pricing around seasonality, not weather.

# COMMAND ----------
display(spark.sql(f"""
SELECT property_type, count(*) AS n, round(avg(base_price)) AS avg_price
FROM {SOURCE}.properties GROUP BY property_type ORDER BY n DESC
"""))

# COMMAND ----------
display(spark.sql(f"""
SELECT d.destination, d.country, count(*) AS n_props, round(avg(p.base_price)) AS avg_price
FROM {SOURCE}.properties p
JOIN {SOURCE}.destinations d ON p.destination_id = d.destination_id
GROUP BY d.destination, d.country
ORDER BY n_props DESC LIMIT 15
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 3. ⭐ The business insight — demand spikes, price doesn't
# MAGIC **This single query is the entire pitch.** Bookings by month vs. the average nightly amount.
# MAGIC Watch: booking volume explodes in June–July (~10× January) while the average price stays
# MAGIC flat (~$550) all year. That gap is money left on the table — and PriceWise closes it.

# COMMAND ----------
display(spark.sql(f"""
SELECT month(check_in) AS mth,
       count(*)                  AS bookings,
       round(avg(total_amount))  AS avg_booking_amount
FROM {SOURCE}.bookings
GROUP BY month(check_in)
ORDER BY mth
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC Bar-chart tip: on the result above, click the **+ (Visualization)** → Bar, X = `mth`,
# MAGIC Y = `bookings`. The July spike is the image to screenshot for the demo.

# COMMAND ----------
display(spark.sql(f"""
SELECT status, count(*) AS n,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM {SOURCE}.bookings GROUP BY status ORDER BY n DESC
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 4. Reviews — a trap, and a lesson
# MAGIC Intuition says "search over reviews." **The data says no.** There are ~99k review rows but
# MAGIC only a handful of *distinct* comments — they're templated. So reviews feed a numeric
# MAGIC `rating` signal only; **descriptions** are our semantic corpus. Prove it:

# COMMAND ----------
display(spark.sql(f"""
SELECT count(*) AS total_reviews,
       count(DISTINCT comment) AS distinct_comments,
       round(avg(rating), 2)   AS avg_rating
FROM {SOURCE}.reviews WHERE is_deleted = false
"""))

# COMMAND ----------
display(spark.sql(f"""
SELECT count(*) AS times_repeated, substr(comment,1,70) AS comment
FROM {SOURCE}.reviews WHERE is_deleted = false
GROUP BY comment ORDER BY times_repeated DESC LIMIT 8
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ### Descriptions ARE unique (our embedding text)
# MAGIC Contrast with above: descriptions are essentially all distinct. This is what we embed.

# COMMAND ----------
display(spark.sql(f"""
SELECT count(*) AS n_props,
       count(DISTINCT description) AS distinct_descriptions,
       round(avg(length(description))) AS avg_len
FROM {SOURCE}.properties
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 5. Clickstream — a demand proxy (with a caveat)
# MAGIC 100k events we can use as *relative popularity* per property. **Caveat:** they're all on a
# MAGIC single day — a snapshot, not a time series. So we treat it as "how hot is this listing"
# MAGIC (relative), not "trend over time."

# COMMAND ----------
display(spark.sql(f"""
SELECT event, count(*) AS n FROM {SOURCE}.clickstream GROUP BY event ORDER BY n DESC
"""))

# COMMAND ----------
display(spark.sql(f"""
WITH per_prop AS (
  SELECT property_id, count(*) AS events FROM {SOURCE}.clickstream GROUP BY property_id
)
SELECT round(avg(events),1) AS avg_events_per_prop,
       min(events) AS min_ev,
       round(percentile_approx(events,0.5)) AS median_ev,
       max(events) AS max_ev,
       count(*) AS props_with_events
FROM per_prop
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 6. Destinations — country + editorial guides
# MAGIC `destinations` gives us `country` (the join key for FX + holidays in notebook 02) and a rich
# MAGIC `description` guide we can fold into the embedding text for better semantic search.

# COMMAND ----------
display(spark.sql(f"""
SELECT destination_id, destination, state_or_province, country,
       length(description) AS guide_len
FROM {SOURCE}.destinations ORDER BY destination
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 7. Amenities — structured filters
# MAGIC `amenities` + `property_amenities` become the `WHERE`-clause filters in hybrid search
# MAGIC (pet-friendly, pool, hot tub, wifi…).

# COMMAND ----------
display(spark.sql(f"SELECT * FROM {SOURCE}.amenities ORDER BY category, name"))

# COMMAND ----------
display(spark.sql(f"""
SELECT a.name, count(*) AS n_properties
FROM {SOURCE}.property_amenities pa
JOIN {SOURCE}.amenities a ON pa.amenity_id = a.amenity_id
GROUP BY a.name ORDER BY n_properties DESC LIMIT 20
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ## ✅ Checkpoint — you should now be able to say:
# MAGIC - **properties** → catalog we search & price (embed `description`, anchor on `base_price`, geo via lat/long).
# MAGIC - **bookings** → the seasonality insight (10× summer) + occupancy/revenue.
# MAGIC - **reviews** → numeric `rating` only (comments are templated — don't embed them).
# MAGIC - **clickstream** → relative popularity (snapshot, not trend).
# MAGIC - **destinations** → `country` join key (FX/holidays) + editorial guide text.
# MAGIC - **amenities** → structured search filters.
# MAGIC
# MAGIC **Next:** `02_ingest_fx_holidays.py` — pull FX + public holidays and land them as Delta tables
# MAGIC in `hackathon.data_axle`.
