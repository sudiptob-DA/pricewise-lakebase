# Databricks notebook source
# MAGIC %md
# MAGIC # PriceWise · 09 · Genie analytics + Lakehouse Sync (write-back loop)
# MAGIC
# MAGIC **Two goals, both breadth pillars:**
# MAGIC 1. **Genie** — a natural-language analytics space over the gold tables, so a host can ask
# MAGIC    *"which of my listings are most under-priced?"* in plain English (powers the Revenue Insights tab).
# MAGIC 2. **Lakehouse Sync** — replicate the app's write-back state (`saved_properties`) from Lakebase
# MAGIC    **back into Unity Catalog** as Delta (SCD Type 2), closing the loop: app writes → lakehouse
# MAGIC    analytics → better features.
# MAGIC
# MAGIC Prereqs: notebooks 03 (gold) + 05 (Lakebase `saved_properties`).

# COMMAND ----------
# MAGIC %md
# MAGIC ## Part A — Prep gold views for Genie
# MAGIC Genie answers best over clean, well-named tables/views with clear columns. We create a couple of
# MAGIC analytics views that map directly to the questions we want to demo.

# COMMAND ----------
CAT, SCH = "hackathon", "data_axle"
spark.sql(f"USE CATALOG {CAT}"); spark.sql(f"USE SCHEMA {SCH}")

# Under-pricing view: how much upside each listing has at peak vs base
spark.sql(f"""
CREATE OR REPLACE VIEW {CAT}.{SCH}.v_underpricing AS
SELECT property_id, destination, country, property_type, target_month,
       base_price, suggested_price,
       round(suggested_price - base_price, 2) AS uplift_dollars,
       round(100*(suggested_price - base_price)/nullif(base_price,0), 1) AS uplift_pct
FROM {CAT}.{SCH}.gold_property_features
""")

# Revenue-by-destination-and-month view (seasonality story)
spark.sql(f"""
CREATE OR REPLACE VIEW {CAT}.{SCH}.v_market_by_destination AS
SELECT destination, country, target_month,
       count(*) AS listings,
       round(avg(base_price)) AS avg_base_price,
       round(avg(suggested_price)) AS avg_suggested_price,
       round(avg(suggested_price - base_price), 2) AS avg_uplift
FROM {CAT}.{SCH}.gold_property_features
GROUP BY destination, country, target_month
""")
display(spark.sql(f"SELECT * FROM {CAT}.{SCH}.v_underpricing ORDER BY uplift_pct DESC LIMIT 8"))

# COMMAND ----------
# MAGIC %md
# MAGIC ## Create the Genie space (UI — one time)
# MAGIC Genie spaces are created in the UI (Genie is a workspace surface, not a pure-API object).
# MAGIC 1. Left sidebar → **Genie** → **New**.
# MAGIC 2. **Name:** `PriceWise Analytics`.
# MAGIC 3. **Tables/views:** add `hackathon.data_axle.v_underpricing`,
# MAGIC    `hackathon.data_axle.v_market_by_destination`, and `gold_property_features`.
# MAGIC 4. Add **sample questions** (below) as starters, then Save.
# MAGIC
# MAGIC ### Demo questions to seed
# MAGIC - "Which 10 listings have the highest pricing upside in July?"
# MAGIC - "What's the average suggested uplift by destination?"
# MAGIC - "Which destinations have the biggest summer demand spike?"
# MAGIC - "Show revenue upside for Phuket by month."
# MAGIC
# MAGIC ### Wiring Genie into the app (Revenue Insights tab)
# MAGIC The app can call the Genie Conversation API with the space_id, or (simplest for the hackathon)
# MAGIC embed the space. Note the `space_id` from the Genie URL and drop it into the app's env if you
# MAGIC wire the live call; otherwise the Insights tab links out to the space.

# COMMAND ----------
# MAGIC %md
# MAGIC ## Part B — Lakehouse Sync: Lakebase → UC (the write-back loop)
# MAGIC The app writes user actions (a saved listing = a row in Lakebase `saved_properties`). Lakehouse
# MAGIC Sync (Change Data Feed) replicates that operational state **back into UC as Delta, SCD Type 2** —
# MAGIC no external Spark job. That makes app engagement available for analytics and feature retraining.
# MAGIC
# MAGIC ### How it works (say this in the demo)
# MAGIC - Lakebase decodes its logical WAL and writes Delta files directly (wal2delta) — runs inside
# MAGIC   Lakebase compute, no pipeline.
# MAGIC - Every change is appended with system columns: `_pg_change_type` (insert/update/delete),
# MAGIC   `_pg_lsn`, `_pg_xid`, `_timestamp`, `_sort_by`. An UPDATE emits pre- and post-image rows.
# MAGIC - Current state = a "latest-value" view (window on `_sort_by`).
# MAGIC
# MAGIC ### Set up (UI — one time)
# MAGIC For Autoscaling `postgres` projects, configure Lakehouse Sync from the Lakebase project:
# MAGIC **Lakebase project → the branch/table → enable Sync to Unity Catalog** (creates a governed Delta
# MAGIC table in `hackathon.data_axle`). Point it at `saved_properties`. Target table name e.g.
# MAGIC `hackathon.data_axle.saved_properties_cdf`.

# COMMAND ----------
# MAGIC %md
# MAGIC ## Verify the write-back (after enabling sync + saving a property in the app)
# MAGIC 1. In the app, click **Save** on a listing (writes a row to Lakebase `saved_properties`).
# MAGIC 2. Wait for the sync interval, then query the synced Delta table in UC below.

# COMMAND ----------
SYNCED_CDF = f"{CAT}.{SCH}.saved_properties_cdf"   # name you chose when enabling Lakehouse Sync
try:
    display(spark.sql(f"""
      SELECT * FROM {SYNCED_CDF} ORDER BY _sort_by DESC LIMIT 20
    """))
    # latest-value view (current saved set), collapsing SCD2 history
    spark.sql(f"""
      CREATE OR REPLACE VIEW {CAT}.{SCH}.v_saved_current AS
      WITH ranked AS (
        SELECT *, row_number() OVER (PARTITION BY save_id ORDER BY _sort_by DESC) rn
        FROM {SYNCED_CDF}
      )
      SELECT * FROM ranked WHERE rn = 1 AND _pg_change_type <> 'delete'
    """)
    print("✅ Lakehouse Sync verified; latest-value view v_saved_current created.")
except Exception as e:
    print("Synced CDF table not found yet — enable Lakehouse Sync on saved_properties in the")
    print("Lakebase project UI, save a listing in the app, then re-run. Detail:", str(e)[:160])

# COMMAND ----------
# MAGIC %md
# MAGIC ## ✅ Checkpoint
# MAGIC - **Genie space** over gold answers plain-English pricing/market questions (Revenue Insights).
# MAGIC - **Lakehouse Sync** streams app write-backs (`saved_properties`) into UC as SCD2 Delta — the loop
# MAGIC   is closed: operational writes flow back to the lakehouse for analytics and retraining.
# MAGIC
# MAGIC **This completes the four Track-2 pillars:** Lakebase Search (05), real-time serving (07),
# MAGIC Reverse ETL / Synced Tables (06), and Lakehouse Sync (09) — plus Genie, Model Serving, and the App.
# MAGIC
# MAGIC **Next:** deploy the app via Asset Bundle (Phase 10) and rehearse `DEMO_SCRIPT.md`.
