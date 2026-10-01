# Deploying PriceWise as a Databricks App

The app runs locally today via uvicorn. This deploys it as a managed **Databricks App** using the
Asset Bundle (`databricks.yml`). Requires Databricks CLI ≥ 0.250.0 (`databricks -v`).

## 1. Validate + deploy the bundle
From the `pricewise/` folder (where `databricks.yml` lives):
```bash
databricks bundle validate
databricks bundle deploy -t dev
```
This uploads the bundle and creates the `pricewise` app resource. It does **not** start the app yet.

## 2. Deploy (start) the app
```bash
databricks bundle run pricewise_app
# or: databricks apps deploy pricewise --source-code-path <workspace path shown in summary>
```
Get the app URL:
```bash
databricks bundle summary
```

## 3. ⚠️ Grant the app's service principal access (the step everyone forgets)
A deployed app runs as its **own service principal**, not as you. It needs:

### a) A Postgres role on the Lakebase project `pricewise-db`
Find the app's service principal **client id** (shown in the app's page in the workspace, or
`databricks bundle summary`). Then in the **Lakebase SQL Editor** (connected to `databricks_postgres`):
```sql
CREATE EXTENSION IF NOT EXISTS databricks_auth;
SELECT databricks_create_role('<app-service-principal-client-id>', 'SERVICE_PRINCIPAL');
GRANT CONNECT ON DATABASE databricks_postgres TO "<app-sp-client-id>";
GRANT USAGE ON SCHEMA public TO "<app-sp-client-id>";
GRANT USAGE ON SCHEMA data_axle TO "<app-sp-client-id>";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO "<app-sp-client-id>";
GRANT SELECT ON ALL TABLES IN SCHEMA data_axle TO "<app-sp-client-id>";
-- the app also WRITES pricing decisions:
GRANT INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO "<app-sp-client-id>";
```
(The app's `db.py` resolves the endpoint and mints OAuth creds via the SDK — on a deployed app the SDK
uses the app's service-principal identity automatically.)

### b) Access to the embedding serving endpoint
Grant the app SP `CAN_QUERY` on `databricks-gte-large-en` (Serving → endpoint → Permissions), so
search embeddings work.

### c) Unity Catalog read on the gold schema
Grant the app SP `USE CATALOG` on `hackathon`, `USE SCHEMA` on `data_axle`, and `SELECT` on the
tables/views it reads (handled by the Postgres grants above for the synced tables; UC grants matter if
the app ever queries UC directly).

## 4. Verify
Open the app URL. Hit `/api/health` — it should return
`{"status":"ok","pricing_source":"synced (Reverse ETL)"}`. Then run a search.

## Notes
- `source_code_path: ./app` points at the folder with `app.yaml` (the entrypoint:
  `uvicorn server.main:app --host 0.0.0.0 --port 8000`).
- Images are bundled in `app/static/img/` — no external CDN, so they work once deployed.
- Local dev remains: `cd app && uvicorn server.main:app --port 8000`.
- To tear down: `databricks bundle destroy -t dev`.
