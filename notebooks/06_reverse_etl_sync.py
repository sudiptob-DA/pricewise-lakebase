# Databricks notebook source
# MAGIC %md
# MAGIC # PriceWise · 06 · Reverse ETL — Synced Tables (UC gold → Lakebase)
# MAGIC
# MAGIC **Goal:** stop hand-loading Lakebase (notebook 05) and instead let **Synced Tables** keep it fed
# MAGIC automatically from the gold tables. This is the **Reverse ETL** pillar — "the last mile" that moves
# MAGIC gold data OUT of the lakehouse and INTO the operational database the app serves from.
# MAGIC
# MAGIC ### Honest note on how to create the sync (read this first)
# MAGIC Our Lakebase project (`pricewise-db`) was created with the **Autoscaling `postgres` API**. Per
# MAGIC Databricks docs, for projects created that way the CLI/SDK create-synced-table path **isn't
# MAGIC available yet — you create synced tables in the UI** (Catalog → source table → Create → Synced
# MAGIC table). So this notebook is: **(1) prep the gold source in UC, (2) create the sync in the UI
# MAGIC (steps below), (3) verify from here.**
# MAGIC
# MAGIC Prereqs: notebooks 03 (gold) + 04 (project + Lakebase Search) done.

# COMMAND ----------
# MAGIC %md
# MAGIC ## 1. Prep the gold source for syncing
# MAGIC Synced tables need a **primary key** on the source. Our serving table is keyed by
# MAGIC `(property_id, target_month)`. We materialize a clean UC table with that PK so the sync maps
# MAGIC cleanly to the Postgres `property_pricing` shape.

# COMMAND ----------
CAT, SCH = "hackathon", "data_axle"
spark.sql(f"USE CATALOG {CAT}"); spark.sql(f"USE SCHEMA {SCH}")

# A sync-ready copy of pricing with an explicit PK and only the columns the app serves.
spark.sql(f"""
CREATE OR REPLACE TABLE {CAT}.{SCH}.serve_property_pricing AS
SELECT property_id, target_month, base_price, suggested_price,
       season_index, season_uplift, comp_uplift, fx_uplift, holiday_uplift,
       popularity_pct AS demand_pct, occupancy_pct
FROM {CAT}.{SCH}.gold_property_features
""")
# Declare the primary key (required for synced tables; enforced logically in UC)
spark.sql(f"ALTER TABLE {CAT}.{SCH}.serve_property_pricing ALTER COLUMN property_id SET NOT NULL")
spark.sql(f"ALTER TABLE {CAT}.{SCH}.serve_property_pricing ALTER COLUMN target_month SET NOT NULL")
try:
    spark.sql(f"""ALTER TABLE {CAT}.{SCH}.serve_property_pricing
                  ADD CONSTRAINT pk_serve_pricing PRIMARY KEY (property_id, target_month)""")
except Exception as e:
    print("PK may already exist:", str(e)[:120])

print("rows:", spark.table(f"{CAT}.{SCH}.serve_property_pricing").count())
display(spark.sql(f"SELECT * FROM {CAT}.{SCH}.serve_property_pricing LIMIT 5"))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 2. Create the synced table (in the UI — one time)
# MAGIC 1. Left sidebar → **Catalog** → `hackathon` → `data_axle` → **`serve_property_pricing`**.
# MAGIC 2. Top-right **Create ▸ Synced table**.
# MAGIC 3. In the dialog:
# MAGIC    - **Target**: your `pricewise-db` project's **`databricks_postgres`** database.
# MAGIC    - **Primary key**: `property_id, target_month` (should auto-detect from the PK we set).
# MAGIC    - **Sync mode**: pick per the decision below.
# MAGIC 4. Create. When it finishes, a read-only table appears in Lakebase as
# MAGIC    **`data_axle.serve_property_pricing_synced`** (schema = UC schema name).
# MAGIC
# MAGIC ### Sync-mode decision (say this in the demo)
# MAGIC | Mode | Update lag | Cost | Use when |
# MAGIC |---|---|---|---|
# MAGIC | **Snapshot** | one-time | cheapest | demo / infrequent refresh ← **start here** |
# MAGIC | **Triggered** | on-demand / scheduled | balanced | "daily refresh" job (our production story) |
# MAGIC | **Continuous** | ~15s | priciest | live streaming feed |
# MAGIC
# MAGIC For the hackathon: **Snapshot** (or Triggered with a manual "refresh"). The daily-refresh job is
# MAGIC the production narrative; the button/snapshot stands in for it in the demo.

# COMMAND ----------
# MAGIC %md
# MAGIC ## 3. Verify the sync landed in Lakebase
# MAGIC Connect to Lakebase and read the synced table. It should have the same row count as the UC source.
# MAGIC (Note the `_synced` suffix and that the schema is the UC schema name, `data_axle`.)

# COMMAND ----------
# MAGIC %pip install --quiet "databricks-sdk>=0.89.0" "psycopg[binary]>=3.1.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------
import psycopg
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
br = next(iter(w.postgres.list_branches(parent="projects/pricewise-db")))
ep = next(iter(w.postgres.list_endpoints(parent=br.name)))
PGHOST = ep.status.hosts.host
PGUSER = w.current_user.me().user_name

def connect():
    tok = w.postgres.generate_database_credential(endpoint=ep.name).token
    return psycopg.connect(host=PGHOST, dbname="databricks_postgres", user=PGUSER, port="5432",
                           password=tok, sslmode="require", autocommit=True)

SYNCED = 'data_axle.serve_property_pricing_synced'   # schema.table as created by the sync
try:
    with connect() as c, c.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM {SYNCED}')
        print(f"✅ synced rows in Lakebase: {cur.fetchone()[0]}")
        cur.execute(f'SELECT property_id, target_month, suggested_price FROM {SYNCED} LIMIT 5')
        for r in cur.fetchall():
            print("  ", r)
except Exception as e:
    print("Synced table not found yet — create it in the UI (step 2), then re-run this cell.")
    print("Detail:", str(e)[:200])

# COMMAND ----------
# MAGIC %md
# MAGIC ## 4. Point the app at the synced table (optional swap)
# MAGIC Notebook 05 loaded `property_pricing` manually. Once the sync works, the app could read
# MAGIC `serve_property_pricing_synced` instead — same columns, now auto-refreshed by Reverse ETL.
# MAGIC For the demo you can keep the manual table (it's identical) and *talk to* the synced table as the
# MAGIC production path, or switch `queries.py` to read the `_synced` table. Either is defensible.
# MAGIC
# MAGIC ## ✅ Checkpoint
# MAGIC - UC gold has a PK'd, sync-ready `serve_property_pricing`.
# MAGIC - A Synced Table (Snapshot/Triggered) replicates it into Lakebase automatically — the **Reverse
# MAGIC   ETL pillar**, no custom pipeline.
# MAGIC - Verified the rows landed in Postgres.
# MAGIC
# MAGIC **Next:** `07_online_feature_serving.py` — serve the pricing features as a real-time Feature Store
# MAGIC endpoint (the "real-time serving is the goal" pillar).
