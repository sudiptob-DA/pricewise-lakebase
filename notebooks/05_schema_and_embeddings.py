# Databricks notebook source
# MAGIC %md
# MAGIC # PriceWise · 05 · Lakebase schema + embeddings + hybrid search
# MAGIC
# MAGIC **Goal:** stand up the operational tables in Lakebase, load the gold data, embed the property
# MAGIC docs (Model Serving), build the native Lakebase Search indexes, and run the **first real hybrid
# MAGIC query** — semantic (`lakebase_vector`) + keyword BM25 (`lakebase_text`) + geo (PostGIS), fused
# MAGIC with RRF, all in one Postgres.
# MAGIC
# MAGIC **Confirmed in notebook 04 (Plan A):**
# MAGIC - Vector: `VECTOR(1024)` + `USING lakebase_ann (embedding vector_cosine_ops)`, query `embedding <=> :vec`
# MAGIC - BM25: `USING lakebase_bm25 (body_tsv)`, rank `body_tsv <@> to_bm25query(to_tsvector('english', :q), 'idx')` (lower = better)
# MAGIC - Geo: PostGIS `geography` + `ST_DWithin`
# MAGIC - Embeddings: `databricks-gte-large-en` (1024-dim), via Model Serving
# MAGIC
# MAGIC **Prereqs:** notebooks 03 (gold tables) + 04 (project provisioned, Lakebase Search enabled) done.

# COMMAND ----------
# MAGIC %pip install --quiet "databricks-sdk>=0.89.0" "psycopg[binary]>=3.1.0"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------
# MAGIC %md
# MAGIC ## 0. Connect to Lakebase (same pattern as notebook 04)
# MAGIC Re-discovers the project endpoint and opens a psycopg connection with a fresh OAuth credential.

# COMMAND ----------
import psycopg
from databricks.sdk import WorkspaceClient

PROJECT_ID = "pricewise-db"
CATALOG, SCHEMA = "hackathon", "data_axle"          # lakehouse source (gold tables)
EMBED_ENDPOINT = "databricks-gte-large-en"          # 1024-dim (confirmed in checks)
EMBED_DIM      = 1024

w = WorkspaceClient()
branch = next(iter(w.postgres.list_branches(parent=f"projects/{PROJECT_ID}")))
endpoint = next(iter(w.postgres.list_endpoints(parent=branch.name)))
ENDPOINT_NAME = endpoint.name
PGHOST = endpoint.status.hosts.host if (endpoint.status and endpoint.status.hosts) else None
PGUSER = w.current_user.me().user_name
assert PGHOST, "Endpoint host not ready — re-run notebook 04 §1 (compute may be waking)."

def fresh_token():
    return w.postgres.generate_database_credential(endpoint=ENDPOINT_NAME).token

def connect():
    return psycopg.connect(host=PGHOST, dbname="databricks_postgres", user=PGUSER,
                           port="5432", password=fresh_token(), sslmode="require", autocommit=True)

with connect() as c, c.cursor() as cur:
    cur.execute("SELECT current_user, current_database()")
    print("Connected:", cur.fetchone())

# COMMAND ----------
# MAGIC %md
# MAGIC ## 1. Create the operational schema
# MAGIC Three tables:
# MAGIC - **`properties`** — searchable catalog: vector + tsvector + geography + structured filter cols.
# MAGIC - **`property_pricing`** — the real-time serving table, keyed by `(property_id, target_month)`.
# MAGIC - **`saved_properties`** — example OLTP user state (a save = a row); write-back source for Lakehouse Sync.

# COMMAND ----------
DDL = f"""
CREATE EXTENSION IF NOT EXISTS lakebase_vector CASCADE;
CREATE EXTENSION IF NOT EXISTS lakebase_text;
CREATE EXTENSION IF NOT EXISTS postgis;

DROP TABLE IF EXISTS properties CASCADE;
CREATE TABLE properties (
  property_id    BIGINT PRIMARY KEY,
  title          TEXT,
  property_type  TEXT,
  destination    TEXT,
  country        TEXT,
  amenities      TEXT,
  base_price     REAL,
  lat            DOUBLE PRECISION,
  lon            DOUBLE PRECISION,
  geo            GEOGRAPHY(Point, 4326),
  search_text    TEXT,
  body_tsv       TSVECTOR,
  embedding      VECTOR({EMBED_DIM})
);

DROP TABLE IF EXISTS property_pricing CASCADE;
CREATE TABLE property_pricing (
  property_id     BIGINT,
  target_month    INT,
  base_price      REAL,
  suggested_price REAL,
  season_index    REAL,
  season_uplift   REAL,
  comp_uplift     REAL,
  fx_uplift       REAL,
  holiday_uplift  REAL,
  demand_pct      REAL,
  occupancy_pct   REAL,
  PRIMARY KEY (property_id, target_month)
);

DROP TABLE IF EXISTS saved_properties CASCADE;
CREATE TABLE saved_properties (
  save_id     BIGSERIAL PRIMARY KEY,
  user_id     TEXT,
  property_id BIGINT,
  saved_at    TIMESTAMPTZ DEFAULT now()
);
"""
with connect() as c, c.cursor() as cur:
    cur.execute(DDL)
print("✅ schema created")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 2. Pull gold data from the lakehouse
# MAGIC We read the gold tables (Spark) into pandas so we can push rows into Postgres. The dataset is
# MAGIC small (18k properties; ~218k pricing rows) so this fits comfortably in the driver.

# COMMAND ----------
doc = spark.table(f"{CATALOG}.{SCHEMA}.gold_property_doc").toPandas()
# one representative feature row per property for the properties table (base_price, geo, type)
feat_any = spark.sql(f"""
  SELECT property_id, any_value(property_type) property_type, any_value(destination) destination,
         any_value(country) country, any_value(base_price) base_price,
         any_value(lat) lat, any_value(lon) lon,
         any_value(popularity_pct) demand_pct, any_value(occupancy_pct) occupancy_pct
  FROM {CATALOG}.{SCHEMA}.gold_property_features GROUP BY property_id
""").toPandas()
pricing = spark.table(f"{CATALOG}.{SCHEMA}.gold_property_features").select(
    "property_id","target_month","base_price","suggested_price","season_index",
    "season_uplift","comp_uplift","fx_uplift","holiday_uplift").toPandas()

props = doc.merge(feat_any, on="property_id", how="inner", suffixes=("","_f"))
print("properties to load:", len(props), "| pricing rows:", len(pricing))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 3. Embed the property docs (Model Serving)
# MAGIC We call `databricks-gte-large-en` in batches. Each `search_text` (title + description +
# MAGIC destination + amenities, from notebook 03) becomes a 1024-dim vector.

# COMMAND ----------
def embed_batch(texts):
    resp = w.serving_endpoints.query(name=EMBED_ENDPOINT, input=texts)
    return [d.embedding for d in resp.data]

texts = props["search_text"].fillna("").tolist()
vectors = []
B = 64
for i in range(0, len(texts), B):
    vectors.extend(embed_batch(texts[i:i+B]))
    if (i // B) % 20 == 0:
        print(f"embedded {min(i+B, len(texts))}/{len(texts)}")
assert len(vectors) == len(props) and len(vectors[0]) == EMBED_DIM
props["embedding"] = vectors
print("✅ embeddings done:", len(vectors), "x", len(vectors[0]))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 4. Load `properties` into Lakebase
# MAGIC Insert rows with the vector (as a pgvector literal), tsvector (built in SQL), and geography point.
# MAGIC We use `execute_many`-style batching for speed.

# COMMAND ----------
def vec_literal(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"

rows = []
for _, r in props.iterrows():
    rows.append((
        int(r["property_id"]), r.get("title"), r.get("property_type"), r.get("destination"),
        r.get("country"), r.get("amenities"), float(r["base_price"]) if r.get("base_price") is not None else None,
        float(r["lat"]) if r.get("lat") is not None else None,
        float(r["lon"]) if r.get("lon") is not None else None,
        r.get("search_text") or "", vec_literal(r["embedding"]),
    ))

INSERT = """
INSERT INTO properties
  (property_id,title,property_type,destination,country,amenities,base_price,lat,lon,search_text,body_tsv,geo,embedding)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
        to_tsvector('english', %s),
        CASE WHEN %s IS NULL OR %s IS NULL THEN NULL ELSE ST_MakePoint(%s,%s)::geography END,
        %s::vector)
ON CONFLICT (property_id) DO NOTHING
"""
with connect() as c, c.cursor() as cur:
    for (pid,title,ptype,dest,country,amen,bp,lat,lon,stext,vlit) in rows:
        cur.execute(INSERT, (pid,title,ptype,dest,country,amen,bp,lat,lon,stext,
                             stext, lon,lat, lon,lat, vlit))
    cur.execute("SELECT count(*) FROM properties")
    print("✅ properties loaded:", cur.fetchone()[0])

# COMMAND ----------
# MAGIC %md
# MAGIC ## 5. Load `property_pricing` (all months)

# COMMAND ----------
prows = [(int(r.property_id), int(r.target_month), float(r.base_price), float(r.suggested_price),
          float(r.season_index), float(r.season_uplift), float(r.comp_uplift),
          float(r.fx_uplift), float(r.holiday_uplift))
         for r in pricing.itertuples(index=False)]
PINS = """INSERT INTO property_pricing
  (property_id,target_month,base_price,suggested_price,season_index,season_uplift,comp_uplift,fx_uplift,holiday_uplift)
  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
  ON CONFLICT (property_id,target_month) DO NOTHING"""
with connect() as c, c.cursor() as cur:
    cur.executemany(PINS, prows)
    cur.execute("SELECT count(*) FROM property_pricing")
    print("✅ pricing rows loaded:", cur.fetchone()[0])

# COMMAND ----------
# MAGIC %md
# MAGIC ## 6. Build the search indexes
# MAGIC - **Vector:** `lakebase_ann` with cosine ops.
# MAGIC - **BM25:** `lakebase_bm25` on the tsvector (built AFTER load — BM25 computes corpus stats at build time).
# MAGIC - **Geo:** GiST on the geography column for fast `ST_DWithin`.

# COMMAND ----------
with connect() as c, c.cursor() as cur:
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prop_vec ON properties USING lakebase_ann (embedding vector_cosine_ops)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prop_bm25 ON properties USING lakebase_bm25 (body_tsv)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prop_geo ON properties USING gist (geo)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pricing_pk ON property_pricing (property_id, target_month)")
print("✅ indexes built")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 7. ⭐ First real HYBRID search
# MAGIC Semantic (vector) + keyword (BM25) fused by RRF, joined to the live suggested price for the month.
# MAGIC Query: *"romantic sea view escape with a pool"* for July (month 7).

# COMMAND ----------
QUERY = "romantic sea view escape with a pool and great nightlife"
MONTH = 7

qvec = vec_literal(embed_batch([QUERY])[0])

HYBRID = f"""
WITH vector_ranked AS (
  SELECT property_id, RANK() OVER (ORDER BY embedding <=> %s::vector) AS rank
  FROM properties ORDER BY embedding <=> %s::vector LIMIT 50
),
keyword_ranked AS (
  SELECT property_id, RANK() OVER (ORDER BY body_tsv <@> to_bm25query(to_tsvector('english', %s), 'idx_prop_bm25')) AS rank
  FROM properties
  ORDER BY body_tsv <@> to_bm25query(to_tsvector('english', %s), 'idx_prop_bm25') LIMIT 50
)
SELECT p.property_id, p.title, p.destination, pr.suggested_price,
       COALESCE(1.0/(60+v.rank),0) AS vec_rrf,
       COALESCE(1.0/(60+k.rank),0) AS kw_rrf,
       COALESCE(1.0/(60+v.rank),0)+COALESCE(1.0/(60+k.rank),0) AS rrf_score
FROM properties p
LEFT JOIN vector_ranked  v ON v.property_id = p.property_id
LEFT JOIN keyword_ranked k ON k.property_id = p.property_id
LEFT JOIN property_pricing pr ON pr.property_id = p.property_id AND pr.target_month = %s
WHERE v.property_id IS NOT NULL OR k.property_id IS NOT NULL
ORDER BY rrf_score DESC, p.property_id
LIMIT 10
"""
with connect() as c, c.cursor() as cur:
    cur.execute(HYBRID, (qvec, qvec, QUERY, QUERY, MONTH))
    print(f"Top hybrid results for: '{QUERY}' (month {MONTH})\n")
    for row in cur.fetchall():
        pid,title,dest,price,vr,kr,score = row
        tag = "both" if vr>0 and kr>0 else ("vector-only" if vr>0 else "keyword-only")
        print(f"  [{score:.4f} {tag:12s}] {title} — {dest} — ${price}")

# COMMAND ----------
# MAGIC %md
# MAGIC ## ✅ Checkpoint
# MAGIC - Lakebase has `properties` (vector + BM25 + geo + filters), `property_pricing` (per-month serving),
# MAGIC   and `saved_properties` (OLTP state).
# MAGIC - Descriptions embedded via Model Serving; native Lakebase Search indexes built.
# MAGIC - A single SQL hybrid query returns semantic + keyword results fused by RRF, with the live price.
# MAGIC
# MAGIC **The "wow" to look for:** results tagged `vector-only` are listings semantic search found that
# MAGIC keyword missed (e.g. a villa whose description implies "romantic sea view" without those exact words).
# MAGIC
# MAGIC **Next:** `06_reverse_etl_sync.py` — wire Synced Tables so gold → Lakebase stays fresh, then the app
# MAGIC reads `properties` + `property_pricing` directly. (Geo 20-mi comps = ENHANCEMENTS E2.)
