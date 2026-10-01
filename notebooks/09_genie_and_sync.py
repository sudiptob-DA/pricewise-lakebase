# Databricks notebook source
# MAGIC %md
# MAGIC # PriceWise · 09 · Genie analytics + Lakebase CDF (write-back loop)
# MAGIC
# MAGIC **Two goals, both breadth pillars:**
# MAGIC 1. **Genie** — a natural-language analytics space over the gold tables, so a host can ask
# MAGIC    *"which of my listings are most under-priced?"* in plain English (powers the Revenue Insights tab).
# MAGIC 2. **Lakebase CDF** — replicate the app's write-back state (`pricing_decisions` — the host's
# MAGIC    Accept/Override actions) from Lakebase Postgres **back into Unity Catalog** as Delta change
# MAGIC    history, closing the loop: app writes → lakehouse analytics → better features.
# MAGIC    (This is the reverse of notebook 06's Synced Tables, which went UC → Postgres.)
# MAGIC
# MAGIC Prereqs: notebooks 03 (gold) + 05 (Lakebase tables) + at least one Accept/Override in the app
# MAGIC (creates the `pricing_decisions` table).

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
# MAGIC Genie spaces are created in the **UI** (Genie is a workspace surface, not a pure-API object).
# MAGIC This notebook builds the analytics **views** Genie reads; you add them to a space in the UI.
# MAGIC
# MAGIC 1. Left sidebar → **Genie** → **New**.
# MAGIC 2. **Name:** `PriceWise Analytics`.
# MAGIC 3. **Tables/views:** add
# MAGIC    - `hackathon.data_axle.v_underpricing` — per-listing upside
# MAGIC    - `hackathon.data_axle.v_market_by_destination` — market/seasonality
# MAGIC    - `hackathon.data_axle.gold_property_features` — the full feature set
# MAGIC    - `hackathon.data_axle.v_decisions_current` — **host Accept/Override decisions** (available
# MAGIC      after you run Part B's Lakebase CDF; add it once it exists)
# MAGIC 4. Add the **sample questions** below as starters, then Save.
# MAGIC
# MAGIC ### Demo questions to seed
# MAGIC *Pricing / market (from gold — available now):*
# MAGIC - "Which 10 listings have the highest pricing upside in July?"
# MAGIC - "What's the average suggested uplift by destination?"
# MAGIC - "Which destinations have the biggest summer demand spike?"
# MAGIC - "Show revenue upside for Phuket by month."
# MAGIC
# MAGIC *Host behavior (from `v_decisions_current` — after Lakebase CDF in Part B):*
# MAGIC - "What's our price-acceptance rate — how often do hosts accept the suggested price?"
# MAGIC - "When hosts override, how far do they deviate from the suggested price on average?"
# MAGIC - "Which destinations have the most overrides?"
# MAGIC
# MAGIC That second set is the payoff of the Accept/Override feature: Genie can now answer questions about
# MAGIC *what hosts actually did with our recommendations*, not just what we recommended.
# MAGIC
# MAGIC ### Wiring Genie into the app (Revenue Insights tab)
# MAGIC The app can call the Genie Conversation API with the space_id, or (simplest for the hackathon)
# MAGIC embed/link the space. Note the `space_id` from the Genie URL; drop it into the app env if you wire
# MAGIC the live call, otherwise the Insights tab links out to the space.

# COMMAND ----------
# MAGIC %md
# MAGIC ## Part B — Lakebase CDF: Lakebase → UC (the write-back loop)
# MAGIC The app writes host actions — an Accept/Override = a row in Lakebase `app_data.pricing_decisions`.
# MAGIC **Lakebase Change Data Feed (CDF)** replicates those Postgres changes **back into UC as Delta** —
# MAGIC no external Spark job. (Note: *Synced Tables* go the other way, UC→Postgres — that was notebook 06.
# MAGIC This is the reverse: Postgres→UC, which is **Lakebase CDF**.)
# MAGIC
# MAGIC ### How it works (say this in the demo)
# MAGIC - CDF is configured **per schema** (every table in `public` is captured), flushing ~every 15s.
# MAGIC - Each row carries system columns: `_pg_change_type` (insert/update/delete), LSN, transaction id,
# MAGIC   and timestamp — SCD2-style history. (We order by `_pg_lsn` for the latest-value view.)
# MAGIC
# MAGIC ### ⚠️ Why a dedicated `app_data` schema (avoid a circular sync)
# MAGIC CDF is configured **per schema**. Our `public` schema holds **synced-from-UC** tables (e.g.
# MAGIC `serve_property_pricing_synced`, the Reverse ETL output from notebook 06). If we ran CDF on
# MAGIC `public`, it would capture those *back* into UC — a pointless round trip (UC → Postgres → UC).
# MAGIC So the app writes its own tables into a separate **`app_data`** schema, and we run CDF on
# MAGIC **`app_data` only**. Clean separation: `public` = data flowing IN, `app_data` = data flowing OUT.
# MAGIC (The app's `_ensure_decisions_table()` already creates `app_data.pricing_decisions`.)
# MAGIC
# MAGIC ### Where does `app_data.pricing_decisions` come from?
# MAGIC The app creates it **lazily on the first Accept/Override** — see `_ensure_decisions_table()` in
# MAGIC `app/server/queries.py`, which runs `CREATE SCHEMA IF NOT EXISTS app_data` +
# MAGIC `CREATE TABLE IF NOT EXISTS app_data.pricing_decisions (...)`. No separate migration; the schema
# MAGIC is self-provisioning. So before CDF has anything to capture, click Accept/Override once in the app.

# COMMAND ----------
# MAGIC %md
# MAGIC ### Cleanup: drop the stale `public.pricing_decisions`
# MAGIC Early builds created `pricing_decisions` in `public` before we moved it to `app_data`. That stale
# MAGIC table must go — otherwise it clutters `public` (which should hold only synced-from-UC tables) and
# MAGIC could be captured if CDF were ever pointed at `public`. The cell below drops it after confirming
# MAGIC the live data is in `app_data`. (Connects to Lakebase like notebook 04/05.)

# COMMAND ----------
# MAGIC %pip install --quiet "databricks-sdk>=0.89.0" "psycopg[binary]>=3.1.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------
import psycopg
from databricks.sdk import WorkspaceClient
_w = WorkspaceClient()
_br = next(iter(_w.postgres.list_branches(parent="projects/pricewise-db")))
_ep = next(iter(_w.postgres.list_endpoints(parent=_br.name)))
_host, _user = _ep.status.hosts.host, _w.current_user.me().user_name
def _conn():
    tok = _w.postgres.generate_database_credential(endpoint=_ep.name).token
    return psycopg.connect(host=_host, dbname="databricks_postgres", user=_user, port="5432",
                           password=tok, sslmode="require", autocommit=True)
with _conn() as c, c.cursor() as cur:
    def cnt(t):
        try: cur.execute(f"SELECT count(*) FROM {t}"); return cur.fetchone()[0]
        except Exception as e: return f"(absent: {str(e)[:40]})"
    print("app_data.pricing_decisions:", cnt("app_data.pricing_decisions"))
    print("public.pricing_decisions  :", cnt("public.pricing_decisions"))
    # Drop the stale public copy (safe: live data is in app_data).
    cur.execute("DROP TABLE IF EXISTS public.pricing_decisions")
    print("✅ dropped public.pricing_decisions (if it existed)")
    print("public.pricing_decisions now:", cnt("public.pricing_decisions"))
# MAGIC
# MAGIC ### Set up (one time)
# MAGIC **Step 1 (OPTIONAL) — replica identity.** This is a *Postgres table-level* setting (WAL verbosity),
# MAGIC **not** a CDF setting. By default Postgres logs only the primary key on UPDATE/DELETE; `FULL` logs
# MAGIC the complete before/after row.
# MAGIC - **For PriceWise you likely DON'T need it:** `pricing_decisions` is append-only (each decision is a
# MAGIC   new INSERT) and our `v_decisions_current` only keeps the latest row per `decision_id` — we never
# MAGIC   diff old-vs-new columns. The default identity is sufficient.
# MAGIC - **Set it only if** you later want full before/after snapshots on updates/deletes:
# MAGIC   ```sql
# MAGIC   ALTER TABLE app_data.pricing_decisions REPLICA IDENTITY FULL;   -- optional
# MAGIC   ```
# MAGIC **Step 2 — start the CDF feed** from the Lakebase project UI:
# MAGIC 1. Open the **`pricewise-db`** project.
# MAGIC 2. Click the **branch name** in the top breadcrumb → **Branch overview**.
# MAGIC 3. Open the **Lakebase CDF** tab → **Start**.
# MAGIC 4. Source schema **`app_data`** → destination catalog **`hackathon`**, destination schema **`data_axle`**.
# MAGIC
# MAGIC The initial snapshot begins immediately. CDF creates a Delta table named with an `lb_` prefix and
# MAGIC `_history` suffix → **`hackathon.data_axle.lb_pricing_decisions_history`**.

# COMMAND ----------
# MAGIC %md
# MAGIC ## Verify the write-back (after Step 1+2 and an Accept/Override in the app)
# MAGIC 1. In the app's Pricing Studio, click **Accept suggested price** or **Apply** an override
# MAGIC    (writes a row to Lakebase `pricing_decisions`).
# MAGIC 2. Wait ~15s for the CDF flush, then query the history table in UC below.

# COMMAND ----------
# CDF names the table lb_<table>_history. Change here only if your CDF used a different name.
SYNCED_CDF = f"{CAT}.{SCH}.lb_pricing_decisions_history"
try:
    # 1) See the raw change feed. CDF rows carry _pg_change_type + an LSN column (_pg_lsn).
    display(spark.sql(f"SELECT * FROM {SYNCED_CDF} LIMIT 20"))

    # 2) Discover the ordering column robustly (CDF column names can vary slightly across versions:
    #    _pg_lsn / _lsn / _commit_lsn, plus a timestamp). Pick the best available to order history.
    cols = [f.name for f in spark.table(SYNCED_CDF).schema.fields]
    order_col = next((c for c in ["_pg_lsn", "_lsn", "_commit_lsn", "_pg_commit_lsn",
                                   "_timestamp", "_commit_timestamp"] if c in cols), None)
    print("CDF columns:", cols)
    print("ordering by:", order_col)
    assert order_col, "No LSN/timestamp column found — inspect the columns printed above and set order_col."

    # 3) Latest-value view: newest change per decision_id, drop deletes. (SCD2 history collapsed.)
    spark.sql(f"""
      CREATE OR REPLACE VIEW {CAT}.{SCH}.v_decisions_current AS
      WITH ranked AS (
        SELECT *, row_number() OVER (PARTITION BY decision_id ORDER BY {order_col} DESC) rn
        FROM {SYNCED_CDF}
      )
      SELECT * FROM ranked WHERE rn = 1 AND _pg_change_type <> 'delete'
    """)

    # 4) The payoff analytic: acceptance rate + how far overrides deviate from the suggested price.
    display(spark.sql(f"""
      SELECT action, count(*) AS n,
             round(avg(applied_price - suggested_price), 2) AS avg_deviation_from_suggested
      FROM {CAT}.{SCH}.v_decisions_current GROUP BY action
    """))
    print("✅ Lakebase CDF verified; v_decisions_current created (acceptance analytics).")
except Exception as e:
    print("CDF history table not found yet. To create it:")
    print("  1) Click Accept/Override in the app once (creates app_data.pricing_decisions).")
    print("  2) pricewise-db project → branch name (breadcrumb) → Branch overview → Lakebase CDF → Start")
    print("     source schema 'app_data' → dest catalog 'hackathon', schema 'data_axle'.")
    print("  3) Wait ~15s, re-run this cell.")
    print("  (Optional: ALTER TABLE app_data.pricing_decisions REPLICA IDENTITY FULL; — only needed")
    print("   if you later want full before/after row snapshots on updates/deletes.)")
    print("Detail:", str(e)[:160])

# COMMAND ----------
# MAGIC %md
# MAGIC ## ✅ Checkpoint
# MAGIC - **Genie space** over gold answers plain-English pricing/market questions, and — after Part B —
# MAGIC   host-behavior questions ("acceptance rate", "override deviation") from `v_decisions_current`.
# MAGIC - **Lakebase CDF** streams app write-backs (`pricing_decisions`) into UC as Delta history — the loop
# MAGIC   is closed: host decisions flow back to the lakehouse for analytics and retraining.
# MAGIC
# MAGIC **Pillars demonstrated:** Lakebase Search (05), Reverse ETL / Synced Tables (06, powering the app),
# MAGIC Lakebase CDF — Postgres→lakehouse (09), plus Genie, Model Serving (embeddings), and the App.
# MAGIC (Real-time feature serving via Feature Store, notebook 07, is optional/skipped — the app already
# MAGIC serves prices live from Lakebase.)
# MAGIC
# MAGIC **Next:** deploy the app via Asset Bundle (Phase 10) and rehearse `DEMO_SCRIPT.md`.
