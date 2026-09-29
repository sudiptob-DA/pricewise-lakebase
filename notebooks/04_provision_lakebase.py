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
# MAGIC Your workspace exposes the Database Instances API, so we create `pricewise-db` programmatically
# MAGIC with the SDK (REST fallback), wait for it to become available, connect, and run the smoke test —
# MAGIC no UI clicks. If you prefer, you can still create it in the Lakebase UI and skip §1; §2 onward
# MAGIC will reuse whatever instance named `pricewise-db` exists.

# COMMAND ----------
# MAGIC %md
# MAGIC ## 1. Provision `pricewise-db` from the notebook (no UI needed)
# MAGIC Your workspace exposes the **Database Instances API** (verified). We create the instance with the
# MAGIC SDK; if the SDK method name differs by version, we fall back to a direct REST POST (same auth).
# MAGIC **Billable, scales to zero.** Smallest capacity. Re-running is safe — if it already exists we reuse it.

# COMMAND ----------
# MAGIC %pip install --quiet "databricks-sdk>=0.89.0" "psycopg[binary]>=3.1.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------
import time, requests
from databricks.sdk import WorkspaceClient

INSTANCE_NAME = "pricewise-db"          # DNS-compliant: letters + hyphens only
CAPACITY      = "CU_1"                   # smallest; adjust if the API rejects (see printed valid values)

w = WorkspaceClient()
host = w.config.host.rstrip("/")
tok  = w.config.token or w.config.oauth_token().access_token
H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

def rest(method, path, body=None):
    r = requests.request(method, f"{host}{path}", headers=H, json=body, timeout=60)
    return r.status_code, (r.json() if r.text and r.headers.get("content-type","").startswith("application/json") else r.text)

def get_instance():
    code, data = rest("GET", f"/api/2.0/database/instances/{INSTANCE_NAME}")
    return data if code == 200 else None

existing = get_instance()
if existing:
    print(f"✅ instance '{INSTANCE_NAME}' already exists — reusing. state={existing.get('state')}")
else:
    # Try SDK first (cleanest), fall back to REST if the method signature differs.
    created = None
    try:
        from databricks.sdk.service.database import DatabaseInstance
        created = w.database.create_database_instance(
            DatabaseInstance(name=INSTANCE_NAME, capacity=CAPACITY)).as_dict()
        print("Created via SDK:", created.get("name"), created.get("state"))
    except Exception as e:
        print("SDK create path unavailable, using REST:", str(e)[:160])
        code, data = rest("POST", "/api/2.0/database/instances",
                          {"name": INSTANCE_NAME, "capacity": CAPACITY})
        print(f"REST create -> {code}: {str(data)[:300]}")
        if code >= 400:
            raise SystemExit("Create failed — read the error above (often the capacity value; "
                             "try 'CU_2' or a numeric size per the docs).")

# --- wait for AVAILABLE ---
for _ in range(40):
    inst = get_instance() or {}
    state = inst.get("state", "UNKNOWN")
    print("state:", state)
    if state in ("AVAILABLE", "RUNNING"): break
    if state in ("FAILED", "DELETING"): raise SystemExit(f"Instance in bad state: {state}")
    time.sleep(10)

inst = get_instance() or {}
PGHOST = inst.get("read_write_dns") or inst.get("dns") or inst.get("host")
print("\nInstance ready. PGHOST =", PGHOST)
print("Full instance record keys:", list(inst.keys()))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 2. Connect from the notebook
# MAGIC The SDK mints a short-lived (60-min) OAuth credential used as the Postgres password over SSL.
# MAGIC `PGHOST` came from the instance record above; `PGUSER` is your Databricks identity (you're the
# MAGIC owner/superuser of a freshly created instance).

# COMMAND ----------
import psycopg

PGDATABASE = "databricks_postgres"
PGUSER     = w.current_user.me().user_name   # your email = your Postgres superuser role
PGPORT     = "5432"
assert PGHOST, "PGHOST not resolved from the instance record — check the printed keys above."

def fresh_token():
    """Mint a short-lived Lakebase credential for this instance."""
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
