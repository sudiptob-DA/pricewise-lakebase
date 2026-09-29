# Databricks notebook source
# MAGIC %md
# MAGIC # PriceWise · 04 · Provision Lakebase + verify Search (CRITICAL CHECKPOINT)
# MAGIC
# MAGIC **Goal:** stand up the Lakebase Postgres instance and **prove** the extensions our whole design
# MAGIC depends on actually work here: `lakebase_vector` (ANN), `lakebase_text` (BM25), `postgis` (geo).
# MAGIC
# MAGIC This is the single biggest unknown in the project. Everything after it (schema, embeddings,
# MAGIC hybrid search) assumes these exist. So we verify **before** building on top of them — and if any
# MAGIC is missing, we learn the fallback now, not on day 3.
# MAGIC
# MAGIC > ⚠️ **Provisioning creates a billable resource** on the shared hackathon workspace. It scales to
# MAGIC > zero when idle. Use the smallest capacity.
# MAGIC
# MAGIC ### Why provision in the UI, verify here
# MAGIC New Lakebase instances are **Autoscaling projects** (branches + endpoints), and the exact
# MAGIC create-API is in flux. The reliable path for a hackathon is: **create the instance in the UI**
# MAGIC (3 clicks), then **connect + smoke-test from this notebook**. If you'd rather not use a notebook
# MAGIC at all for the check, you can paste the SQL in §3 straight into the **Lakebase SQL Editor**.

# COMMAND ----------
# MAGIC %md
# MAGIC ## 1. Provision `pricewise-db` (do this once, in the UI)
# MAGIC 1. Left sidebar → **Compute** → **Database instances** (or search "Lakebase") → **Create**.
# MAGIC 2. **Name:** `pricewise-db` · **Capacity:** smallest available (e.g. `CU_1`) · leave defaults.
# MAGIC 3. Wait until status is **Available** (usually 1–3 min; first start from zero can be slower).
# MAGIC 4. Click **Connect** on the instance → copy the **host**, **database**, **user**, and **endpoint
# MAGIC    name** into the config cell below. (The "Connect" dialog shows the exact values for your identity.)
# MAGIC
# MAGIC > Tip: also verify it via the CLI on your laptop:
# MAGIC > `databricks database list-database-instances` (or `databricks postgres ...` for projects).

# COMMAND ----------
# MAGIC %md
# MAGIC ## 2. Connect from the notebook
# MAGIC We use the documented pattern: the Databricks SDK mints a short-lived (60-min) OAuth credential,
# MAGIC used as the Postgres password, over an SSL psycopg connection. Fill in the values from the
# MAGIC **Connect** dialog. Leave `ENDPOINT_NAME` blank if your instance shows a plain host instead of a
# MAGIC `projects/.../endpoints/...` name — the code handles both.

# COMMAND ----------
# MAGIC %pip install --quiet "databricks-sdk>=0.89.0" "psycopg[binary]>=3.1.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------
# ---- PASTE from the Lakebase "Connect" dialog ----
PGHOST        = ""    # e.g. instance-xxxx.database.<region>.cloud.databricks.com
PGDATABASE    = "databricks_postgres"
PGUSER        = ""    # your Databricks identity (email) OR service-principal client id shown in dialog
PGPORT        = "5432"
ENDPOINT_NAME = ""    # projects/<id>/branches/<id>/endpoints/<id>  (blank if not shown)
INSTANCE_NAME = "pricewise-db"

assert PGHOST and PGUSER, "Fill PGHOST and PGUSER from the Lakebase Connect dialog first."

# COMMAND ----------
import psycopg
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()   # uses this notebook's identity (you are the DB owner/superuser)

def fresh_token():
    """Mint a short-lived Lakebase credential; works for both projects (endpoint) and instances."""
    try:
        if ENDPOINT_NAME:
            return w.postgres.generate_database_credential(endpoint=ENDPOINT_NAME).token
    except Exception as e:
        print("postgres.generate_database_credential(endpoint=...) failed:", str(e)[:160])
    # fallback: instances API
    cred = w.database.generate_database_credential(
        request_id=INSTANCE_NAME, instance_names=[INSTANCE_NAME])
    return cred.token

def connect():
    return psycopg.connect(
        host=PGHOST, dbname=PGDATABASE, user=PGUSER, port=PGPORT,
        password=fresh_token(), sslmode="require", autocommit=True)

with connect() as c, c.cursor() as cur:
    cur.execute("SELECT current_user, current_database(), version()")
    print(cur.fetchone())
print("✅ connected to Lakebase")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 3. ⭐ The smoke test — do the search extensions exist?
# MAGIC We try to enable each extension and run a *tiny* real query. Each probe is wrapped so a failure
# MAGIC is **reported, not fatal** — the point is to discover exactly what's available. Read the
# MAGIC PASS/FAIL summary at the end: it decides whether we use native Lakebase Search or the
# MAGIC Postgres-FTS + pgvector fallback.

# COMMAND ----------
results = {}

def probe(name, sql_steps):
    """Run a list of SQL statements; record PASS/FAIL + first error."""
    try:
        with connect() as c, c.cursor() as cur:
            for s in sql_steps:
                cur.execute(s)
            results[name] = ("PASS", "")
            print(f"✅ {name}: PASS")
    except Exception as e:
        results[name] = ("FAIL", str(e)[:200])
        print(f"❌ {name}: FAIL — {str(e)[:200]}")

# --- 3a. enable extensions ---
probe("ext:lakebase_vector", ["CREATE EXTENSION IF NOT EXISTS lakebase_vector CASCADE"])
probe("ext:lakebase_text",   ["CREATE EXTENSION IF NOT EXISTS lakebase_text"])
probe("ext:postgis",         ["CREATE EXTENSION IF NOT EXISTS postgis"])

# --- 3b. what got installed? ---
with connect() as c, c.cursor() as cur:
    cur.execute("SELECT extname FROM pg_extension ORDER BY extname")
    print("Installed extensions:", [r[0] for r in cur.fetchall()])

# COMMAND ----------
# --- 3c. vector probe: create tiny table, insert, ANN order-by ---
probe("query:pgvector <=>", [
    "DROP TABLE IF EXISTS _smoke_vec",
    "CREATE TABLE _smoke_vec (id int, emb vector(3))",
    "INSERT INTO _smoke_vec VALUES (1,'[0.1,0.2,0.3]'),(2,'[0.9,0.8,0.7]')",
    "SELECT id FROM _smoke_vec ORDER BY emb <=> '[0.1,0.2,0.25]' LIMIT 1",
    "DROP TABLE IF EXISTS _smoke_vec",
])

# COMMAND ----------
# --- 3d. BM25 probe (lakebase_text). Exact DDL/operator may vary; try a couple of forms. ---
# Form A: bm25 index + <@> operator (as shown in the hackathon deck)
probe("query:bm25 (form A <@>)", [
    "DROP TABLE IF EXISTS _smoke_txt",
    "CREATE TABLE _smoke_txt (id int, body text)",
    "INSERT INTO _smoke_txt VALUES (1,'romantic sea view villa with pool'),(2,'budget city studio near station')",
    # try a bm25 ranking expression; if the operator/function name differs this FAILs and we learn
    "SELECT id FROM _smoke_txt ORDER BY body <@> bm25('sea view pool') LIMIT 1",
    "DROP TABLE IF EXISTS _smoke_txt",
])

# Form B: native Postgres full-text (guaranteed fallback) — proves hybrid is still possible
probe("query:postgres FTS (fallback)", [
    "DROP TABLE IF EXISTS _smoke_fts",
    "CREATE TABLE _smoke_fts (id int, body text)",
    "INSERT INTO _smoke_fts VALUES (1,'romantic sea view villa with pool'),(2,'budget city studio near station')",
    "SELECT id FROM _smoke_fts WHERE to_tsvector('english', body) @@ websearch_to_tsquery('english','sea view pool') "
    "ORDER BY ts_rank(to_tsvector('english', body), websearch_to_tsquery('english','sea view pool')) DESC LIMIT 1",
    "DROP TABLE IF EXISTS _smoke_fts",
])

# COMMAND ----------
# --- 3e. geo probe (PostGIS ST_DWithin) ---
probe("query:postgis ST_DWithin", [
    "DROP TABLE IF EXISTS _smoke_geo",
    "CREATE TABLE _smoke_geo (id int, g geography(Point,4326))",
    "INSERT INTO _smoke_geo VALUES (1, ST_MakePoint(98.39, 7.88)::geography), (2, ST_MakePoint(2.35,48.85)::geography)",
    "SELECT id FROM _smoke_geo WHERE ST_DWithin(g, ST_MakePoint(98.40,7.89)::geography, 32000)",  # ~20 mi
    "DROP TABLE IF EXISTS _smoke_geo",
])

# COMMAND ----------
# MAGIC %md
# MAGIC ## 4. Verdict — which search path are we on?

# COMMAND ----------
print("="*60)
for k, (status, err) in results.items():
    print(f"{status:4}  {k}" + (f"   ← {err}" if err else ""))
print("="*60)

vec_ok  = results.get("query:pgvector <=>",("FAIL",))[0]      == "PASS"
bm25_ok = results.get("query:bm25 (form A <@>)",("FAIL",))[0] == "PASS"
fts_ok  = results.get("query:postgres FTS (fallback)",("FAIL",))[0] == "PASS"
geo_ok  = results.get("query:postgis ST_DWithin",("FAIL",))[0] == "PASS"

print(f"Vector search : {'lakebase_vector / pgvector ✅' if vec_ok else '❌ MISSING'}")
print(f"Keyword search: {'lakebase_text BM25 ✅ (native path)' if bm25_ok else ('Postgres FTS fallback ✅' if fts_ok else '❌ none')}")
print(f"Geo (comps)   : {'PostGIS ✅' if geo_ok else '❌ MISSING'}")
print()
if vec_ok and bm25_ok:
    print("➡  Plan A: native Lakebase hybrid search (lakebase_vector + lakebase_text + rrf). Proceed as designed.")
elif vec_ok and fts_ok:
    print("➡  Plan B: pgvector + Postgres FTS (ts_rank). Still a valid hybrid — adjust the search SQL in notebook 05.")
else:
    print("➡  Investigate: core search capability missing. Do NOT proceed to schema until resolved.")

# COMMAND ----------
# MAGIC %md
# MAGIC ## ✅ Checkpoint
# MAGIC - `pricewise-db` provisioned and reachable from the notebook.
# MAGIC - We KNOW which of `lakebase_vector`, `lakebase_text`(BM25), `postgis` are available, with a
# MAGIC   guaranteed Postgres-FTS fallback if BM25 isn't.
# MAGIC - Record the connection details (`PGHOST`, `PGUSER`, `ENDPOINT_NAME`) in your `.env` for the app.
# MAGIC
# MAGIC **Also verify (for `ENHANCEMENTS.md`):** while connected, note whether branching/snapshots are
# MAGIC exposed (gates E1/E3). We'll test those only if we pursue the enhancements.
# MAGIC
# MAGIC **Next:** `05_schema_and_embeddings.py` — create the Lakebase tables and populate vectors, using
# MAGIC whichever search path this notebook confirmed.
