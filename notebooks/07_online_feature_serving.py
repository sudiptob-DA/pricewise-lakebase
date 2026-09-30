# Databricks notebook source
# MAGIC %md
# MAGIC # PriceWise · 07 · Real-time feature serving (the Track-2 "goal")
# MAGIC
# MAGIC **Goal:** serve the pricing features from an **Online Feature Store on Lakebase** through a
# MAGIC **Feature Serving endpoint** — a <200ms point lookup by key. Track 2 states *"real-time feature
# MAGIC serving is the goal,"* and this notebook is that pillar, done the official way:
# MAGIC `create_online_store` (which itself provisions a Lakebase instance) → `publish_table` → serving endpoint.
# MAGIC
# MAGIC ### How this differs from the app's Lakebase reads
# MAGIC The app already reads pricing from Lakebase directly (fast). This notebook adds the **managed
# MAGIC Feature Store path**: features published to an online store and served via a governed endpoint you
# MAGIC can `predict()` against — the productized "feature serving" story, with automatic freshness.
# MAGIC
# MAGIC Prereqs: notebook 03 (gold). Uses the Feature Engineering client (Databricks runtime ML or
# MAGIC `databricks-feature-engineering`).

# COMMAND ----------
# MAGIC %pip install --quiet databricks-feature-engineering mlflow
# MAGIC dbutils.library.restartPython()

# COMMAND ----------
# MAGIC %md
# MAGIC ## 1. Build a feature table with a single primary key
# MAGIC Feature lookup is by one key at serving time. We serve pricing for a **specific month**, so we
# MAGIC build a per-property feature table for a chosen month (default July) keyed by `property_id`.
# MAGIC (A production version would key on `(property_id, month)` or serve all months; single-key keeps
# MAGIC the demo lookup crisp.)

# COMMAND ----------
CAT, SCH = "hackathon", "data_axle"
SERVE_MONTH = 7
feature_table = f"{CAT}.{SCH}.fs_property_pricing"

spark.sql(f"""
CREATE OR REPLACE TABLE {feature_table} AS
SELECT property_id,
       base_price, suggested_price, season_index,
       season_uplift, comp_uplift, fx_uplift, holiday_uplift,
       popularity_pct AS demand_pct, occupancy_pct
FROM {CAT}.{SCH}.gold_property_features
WHERE target_month = {SERVE_MONTH}
""")
spark.sql(f"ALTER TABLE {feature_table} ALTER COLUMN property_id SET NOT NULL")
try:
    spark.sql(f"ALTER TABLE {feature_table} ADD CONSTRAINT pk_fs_pricing PRIMARY KEY (property_id)")
except Exception as e:
    print("PK note:", str(e)[:100])
# Enable CDF so TRIGGERED/CONTINUOUS publish modes work later
spark.sql(f"ALTER TABLE {feature_table} SET TBLPROPERTIES (delta.enableChangeDataFeed = 'true')")
print("feature rows:", spark.table(feature_table).count())
display(spark.sql(f"SELECT * FROM {feature_table} LIMIT 5"))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 2. Create an Online Feature Store (provisions a Lakebase instance) + publish
# MAGIC `create_online_store` stands up managed Lakebase-backed serving infra; `publish_table` copies the
# MAGIC feature table into it. This is the official "real-time feature serving is powered by Lakebase" path.

# COMMAND ----------
from databricks.feature_engineering import FeatureEngineeringClient
from databricks.sdk import WorkspaceClient

fe = FeatureEngineeringClient()
w = WorkspaceClient()
user = w.current_user.me().user_name.split("@")[0].replace(".", "-")

online_store_name = f"pricewise-online-{user}"
online_table_name = f"{CAT}.{SCH}.fs_property_pricing_online"

# Create (idempotent-ish: skip if exists)
try:
    fe.create_online_store(name=online_store_name, capacity="CU_2")
    print("creating online store...")
except Exception as e:
    print("create_online_store note:", str(e)[:140])

import time
for _ in range(40):
    store = fe.get_online_store(name=online_store_name)
    print("online store state:", store.state)
    if str(store.state).upper().endswith("AVAILABLE") or "AVAILABLE" in str(store.state).upper():
        break
    time.sleep(15)

fe.publish_table(
    online_store=fe.get_online_store(name=online_store_name),
    source_table_name=feature_table,
    online_table_name=online_table_name,
)
print("✅ published to online store")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 3. Create a FeatureSpec (what the endpoint serves)
# MAGIC A simple lookup spec: given a `property_id`, return its pricing features.

# COMMAND ----------
from databricks.feature_engineering import FeatureLookup

feature_spec_name = f"{CAT}.{SCH}.pricewise_spec"
features = [FeatureLookup(table_name=feature_table, lookup_key="property_id")]
try:
    fe.create_feature_spec(name=feature_spec_name, features=features, exclude_columns=None)
    print("feature spec created")
except Exception as e:
    print("feature spec note:", str(e)[:140] if "already exists" in str(e) else e)

# COMMAND ----------
# MAGIC %md
# MAGIC ## 4. Deploy the Feature Serving endpoint
# MAGIC Scale-to-zero, small workload. This is the endpoint the app (or an agent) calls for a live price.

# COMMAND ----------
from databricks.sdk.service.serving import EndpointCoreConfigInput, ServedEntityInput

endpoint_name = "pricewise-feature-serving"
try:
    w.serving_endpoints.create_and_wait(
        name=endpoint_name,
        config=EndpointCoreConfigInput(
            served_entities=[ServedEntityInput(
                entity_name=feature_spec_name,
                scale_to_zero_enabled=True,
                workload_size="Small",
            )]
        ),
    )
    print("✅ endpoint created:", endpoint_name)
except Exception as e:
    print("endpoint note (may already exist):", str(e)[:160])
print("state:", w.serving_endpoints.get(name=endpoint_name).state)

# COMMAND ----------
# MAGIC %md
# MAGIC ## 5. ⭐ Query it — real-time price by property_id
# MAGIC The payoff: a live feature lookup. Time it — should be well under a second once warm.

# COMMAND ----------
import time, mlflow.deployments
client = mlflow.deployments.get_deploy_client("databricks")

sample_ids = [r["property_id"] for r in
              spark.sql(f"SELECT property_id FROM {feature_table} LIMIT 3").collect()]

for pid in sample_ids:
    t = time.time()
    resp = client.predict(endpoint=endpoint_name,
                          inputs={"dataframe_records": [{"property_id": int(pid)}]})
    ms = int((time.time() - t) * 1000)
    rec = resp["outputs"][0] if "outputs" in resp else resp
    print(f"property {pid}: {ms} ms → suggested_price=", rec.get("suggested_price") if isinstance(rec, dict) else rec)

# COMMAND ----------
# MAGIC %md
# MAGIC ## ✅ Checkpoint
# MAGIC - Pricing features published to a **Lakebase-backed Online Feature Store**.
# MAGIC - A **Feature Serving endpoint** returns a property's price in real time by `property_id`.
# MAGIC - This is the "real-time feature serving is the goal" pillar, done the managed way.
# MAGIC
# MAGIC **Demo framing:** the app can serve prices two ways — a direct Lakebase read (what we built in the
# MAGIC UI) *and* this governed Feature Serving endpoint. Same Lakebase foundation; this is the productized,
# MAGIC agent-callable path.
# MAGIC
# MAGIC > ⚠️ Cost note: the online store + serving endpoint are billable (scale to zero when idle). Delete
# MAGIC > them after the hackathon: `fe.delete_online_store(...)` and `w.serving_endpoints.delete(...)`.
# MAGIC
# MAGIC **Next:** `09_genie_and_sync.py` — a Genie space over gold (analytics) + Lakehouse Sync
# MAGIC (Lakebase → UC) for the app's write-backs.
