# PriceWise App

FastAPI backend + static UI. Reads `properties` and `property_pricing` from Lakebase; embeds
search queries via Model Serving. Serves the multi-tab UI at `/`.

## Run locally
Uses your `~/.databrickscfg` DEFAULT profile for auth (same as the notebooks).

```bash
cd app
pip install -r requirements.txt
uvicorn server.main:app --reload --port 8000
# open http://localhost:8000
```

Prereq: notebooks 03–05 have populated Lakebase (`properties`, `property_pricing`).

## Endpoints
- `GET  /api/health`
- `GET  /api/destinations`
- `GET  /api/search?q=...&month=7&semantic=true&max_price=&destination=`
- `GET  /api/property/{id}`
- `GET  /api/pricing/{id}?month=7`
- `GET  /api/pricing/{id}/curve`
- `GET  /api/comps/{id}?radius_mi=20&month=7`
- `GET  /api/market?month=7`
- `POST /api/save` `{ "property_id": 123 }`

## Deploy as a Databricks App
`app.yaml` is the entrypoint. Deploy via Asset Bundle (Phase 10) or the Apps UI.
The app's service principal needs a Postgres role on `pricewise-db` and access to the
embedding serving endpoint.

## Layout
```
app/
  app.yaml            # Databricks Apps entrypoint
  requirements.txt
  server/
    main.py           # FastAPI routes + static mount
    db.py             # Lakebase pool (OAuth token rotation) + embed()
    queries.py        # all SQL (hybrid search, pricing, comps)
  static/
    index.html        # multi-tab UI (Search / Stay / Pricing Studio)
    app.js
    styles.css
```
