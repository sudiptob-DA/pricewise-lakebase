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
# MAGIC ### Provision + verify entirely from this notebook
# MAGIC Lakebase Autoscaling uses the **projects API** (`w.postgres`), so we create the `pricewise-db`
# MAGIC project programmatically with the SDK, discover its production branch + endpoint, connect, and run
# MAGIC the smoke test — no UI clicks. If you prefer, you can still create it in the Lakebase UI and skip
# MAGIC §1; §2 onward will reuse whatever project named `pricewise-db` exists.
# MAGIC
# MAGIC > **One manual step remains:** Lakebase Search must be **Enabled once** in the project settings
# MAGIC > (see the ⚠️ note in §3) before the vector/text extensions can be installed.

# COMMAND ----------
# MAGIC %md
# MAGIC ## 1. Provision `pricewise-db` from the notebook (no UI needed)
# MAGIC Lakebase Autoscaling uses the **projects API** (`w.postgres`): a project contains branches, each
# MAGIC branch has an endpoint (the R/W compute you connect to). We create the project, then discover its
# MAGIC production branch + primary endpoint. **Billable, scales to zero.** Re-running is safe — if the
# MAGIC project already exists we reuse it.

# COMMAND ----------
# MAGIC %pip install --quiet "databricks-sdk>=0.89.0" "psycopg[binary]>=3.1.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------
import time
from databricks.sdk import WorkspaceClient

PROJECT_ID    = "pricewise-db"          # DNS-compliant: lowercase letters + hyphens only
INSTANCE_NAME = PROJECT_ID               # kept for downstream cell compatibility

w = WorkspaceClient()

# --- Check if project already exists (Autoscaling / w.postgres API) ---
existing = None
try:
    existing = w.postgres.get_project(name=f"projects/{PROJECT_ID}")
    print(f"✅ project '{PROJECT_ID}' already exists — reusing.")
except Exception as e:
    if "NOT_FOUND" in str(e) or "404" in str(e):
        print(f"Project '{PROJECT_ID}' not found — creating...")
    else:
        raise

if not existing:
    from databricks.sdk.service.postgres import Project, ProjectSpec
    op = w.postgres.create_project(
        project=Project(spec=ProjectSpec(display_name=PROJECT_ID, pg_version=17)),
        project_id=PROJECT_ID,
    )
    project = op.wait()   # blocks until ready (usually seconds)
    print("Created:", project.name)
else:
    project = existing

# --- Discover production branch + primary endpoint ---
branch_name = None
for b in w.postgres.list_branches(parent=f"projects/{PROJECT_ID}"):
    branch_name = b.name
    state = b.status.current_state if b.status else "unknown"
    print(f"Branch: {b.name}  state={state}")
    break  # first = production

assert branch_name, "No branches found — project may still be initializing."

PGHOST = None
ENDPOINT_NAME = None
for ep in w.postgres.list_endpoints(parent=branch_name):
    ENDPOINT_NAME = ep.name
    if ep.status and ep.status.hosts:
        PGHOST = ep.status.hosts.host
    print(f"Endpoint: {ep.name}  host={PGHOST}")
    break  # primary endpoint

# If endpoint is waking from scale-to-zero, wait for a host
for _ in range(20):
    if PGHOST:
        break
    print("Waiting for endpoint host...")
    time.sleep(10)
    ep = w.postgres.get_endpoint(name=ENDPOINT_NAME)
    if ep.status and ep.status.hosts:
        PGHOST = ep.status.hosts.host

print(f"\nProject ready. PGHOST = {PGHOST}")
print(f"ENDPOINT_NAME = {ENDPOINT_NAME}")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 2. Connect from the notebook
# MAGIC The SDK mints a short-lived (60-min) OAuth credential used as the Postgres password over SSL.
# MAGIC For Autoscaling projects the credential is scoped to the **endpoint** (`ENDPOINT_NAME`); `PGUSER`
# MAGIC is your Databricks identity (you're the owner/superuser of the project's default database).

# COMMAND ----------
import psycopg

PGDATABASE = "databricks_postgres"
PGUSER     = w.current_user.me().user_name   # your email = your Postgres superuser role
PGPORT     = "5432"
assert PGHOST, "PGHOST not resolved from the endpoint — the compute may still be starting; re-run §1."

def fresh_token():
    """Mint a short-lived Lakebase credential scoped to this project's endpoint."""
    return w.postgres.generate_database_credential(endpoint=ENDPOINT_NAME).token

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
# MAGIC
# MAGIC > ⚠️ **One-time prerequisite:** `lakebase_vector` / `lakebase_text` only become installable after you
# MAGIC > **Enable Lakebase Search** in the project (Lakebase App → your project → **Settings → Lakebase
# MAGIC > Search → Enable**). It restarts computes and is **irreversible**. If these probes fail with
# MAGIC > *"must be loaded via shared_preload_libraries"*, that toggle isn't on yet — flip it, then re-run.

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
# --- 3d. BM25 probe (lakebase_text) — DOCUMENTED syntax: lakebase_bm25 index + to_bm25query() ---
# This is the exact pattern notebook 05 will use, so we certify it here (not the loose bm25('..') form).
# Note: BM25 needs a real lakebase_bm25 index; the operator is <@> to_bm25query(tsvector_query, 'index_name').
# Lower score = more relevant.
probe("query:bm25 (documented to_bm25query)", [
    "DROP TABLE IF EXISTS _smoke_txt",
    "CREATE TABLE _smoke_txt (id int, body text, body_tsv tsvector)",
    "INSERT INTO _smoke_txt VALUES "
    "(1,'romantic sea view villa with pool', to_tsvector('english','romantic sea view villa with pool')),"
    "(2,'budget city studio near station',   to_tsvector('english','budget city studio near station'))",
    "CREATE INDEX _smoke_txt_bm25 ON _smoke_txt USING lakebase_bm25 (body_tsv)",
    "SELECT id FROM _smoke_txt "
    "ORDER BY body_tsv <@> to_bm25query(to_tsvector('english','sea view pool'), '_smoke_txt_bm25') LIMIT 1",
    "DROP TABLE IF EXISTS _smoke_txt",
])

# Form B: native Postgres full-text (guaranteed fallback) — kept as a safety reference
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
bm25_ok = results.get("query:bm25 (documented to_bm25query)",("FAIL",))[0] == "PASS"
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
