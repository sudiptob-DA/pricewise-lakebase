# PriceWise — Build Guide

A step-by-step, learn-as-you-go roadmap. Each phase has a notebook (or script) you run and
understand before moving on. Notebooks live in `notebooks/` as Databricks `.py` files
(`# COMMAND ----------` cell markers) — import them into the workspace, or read them here.

**Legend:** ✅ done · ⬜ to do · 🔎 checkpoint (verify before continuing)

---

## Phase 0 — Foundations ✅
Already wired up:
- Auth via `~/.databrickscfg` (DEFAULT profile, PAT). ✅
- Databricks CLI installed + verified (`current-user me`). ✅
- Repo scaffold + brief + architecture diagram. ✅
- Confirmed workspace: catalog `hackathon`, team schema `data_axle`, warehouse `hackathon-shared-small` (`64d934278b5c472d`), source `samples.wanderbricks`. ✅

---

## Phase 1 — Understand the data ✅  → `notebooks/01_explore_lakehouse.py`
**Goal:** know the raw data cold before transforming it. You'll learn the tables, the
seasonality insight (the whole business case), and why reviews are unusable for search.
- Read `properties`, `reviews`, `bookings`, `clickstream`, `destinations`, `amenities`.
- Prove the wedge: bookings by month (July ≈ 10× January) vs. flat avg price.
- Confirm descriptions are the semantic corpus (distinct) and reviews are templated.
- 🔎 **Checkpoint:** you can explain, in one sentence each, what every table gives us.

## Phase 2 — Ingest external signals ✅  → `notebooks/02_ingest_fx_holidays.py`
**Goal:** pull FX + holidays once, land them as Delta tables in `hackathon.data_axle`.
- Call `frankfurter.dev` (current + 12-mo history) → `fx_rates` Delta table.
- Call `date.nager.at` per destination country → `holidays` Delta table (+ Songkran backfill).
- 🔎 **Checkpoint:** both Delta tables queryable; joins to `destinations.country` work.

## Phase 3 — Build gold features 🔎 (built; run + verify)  → `notebooks/03_gold_features.py`
**Goal:** the feature tables that power pricing + search.
- `gold_property_doc` — title + description + destination context (text to embed).
- `gold_property_features` — season_factor, demand_score, occupancy_30d, comp inputs,
  holiday_boost, fx_factor, and the computed `suggested_price`.
- Explain each factor's formula (transparent heuristic — swappable later).
- 🔎 **Checkpoint:** eyeball 5 properties; the suggested price moves sensibly with each factor.

## Phase 4 — Provision Lakebase + verify Search ✅  → `notebooks/04_provision_lakebase.py`
> **Result:** `pricewise-db` (CU_2) provisioned. Lakebase Search enabled in project settings.
> All extensions PASS — **Plan A: native Lakebase hybrid** (`lakebase_vector` + `lakebase_text` BM25 + PostGIS).
> BM25 syntax = `lakebase_bm25` index + `<@> to_bm25query(to_tsvector('english', q), 'index_name')` (lower = better).
**Goal:** stand up the Postgres instance and CONFIRM the search extensions (the big unknown).
- Provision `pricewise-db` (smallest capacity, scales to zero).
- `CREATE EXTENSION` for `lakebase_vector`, `lakebase_text`, PostGIS.
- Smoke test: trivial `bm25()` + vector query. If unavailable → fall back to `tsvector`/`pgvector`.
- 🔎 **Checkpoint (critical):** we know for certain which search path we're on.

## Phase 5 — Schema + embeddings ✅  → `notebooks/05_schema_and_embeddings.py`
> **Result:** Lakebase tables live (`properties` vector+BM25+geo, `property_pricing`, `saved_properties`);
> 18k docs embedded via `databricks-gte-large-en` (1024-dim); native hybrid search (vector+BM25+RRF)
> returns ranked results with live price. `vector-only` hits confirm the semantic-beats-keyword beat.
>
> **Tuning note (polish, not a blocker):** RRF scores are near-flat and results are all `vector-only` or
> `keyword-only` (no `both` overlap) — the two rankers hit different rows. To sharpen during polish:
> lower RRF constant (60→~20), widen per-list LIMIT (50→100), apply structured filters to concentrate
> both lists on the same candidate pool, and/or weight the rankers. Fine for the demo as-is.
**Goal:** create Lakebase tables and populate vectors.
- DDL: `properties` (vector + text + geography + filters), `property_pricing`, `search_events`, `saved_properties`.
- Embed `gold_property_doc` via Model Serving endpoint; write vectors.
- Build the vector + BM25 + geo indexes.
- 🔎 **Checkpoint:** a manual hybrid query returns sensible ranked results.

## Phase 6 — Reverse ETL (UC → Lakebase) 🔎 (written; UI sync + verify)  → `notebooks/06_reverse_etl_sync.py`
> Note: `pricewise-db` is a `postgres` (Autoscaling) project, so the synced-table CLI/API path isn't
> available — create the sync in the UI (Catalog → serve_property_pricing → Create ▸ Synced table),
> then verify from the notebook.
**Goal:** keep Lakebase fed from gold via Synced Tables.
- Configure Synced Table(s): gold features → `property_pricing`; property doc/embeddings → `properties`.
- Choose sync mode (snapshot/triggered) + the "refresh" trigger for the demo.
- 🔎 **Checkpoint:** update a gold row → see it reflected in Lakebase.

## Phase 7 — Online feature serving 🔎 (written; run to deploy)  → `notebooks/07_online_feature_serving.py`
**Goal:** the "real-time serving is the goal" pillar.
- Publish pricing features to the Online Feature Store on Lakebase.
- Time a point lookup by `property_id` (< 200ms target).
- 🔎 **Checkpoint:** serving latency measured and recorded.

## Phase 8 — The app ⬜  → `app/server/` + `app/static/`
**Goal:** FastAPI backend + multi-tab static UI.
- Endpoints: `/search` (hybrid), `/property/{id}`, `/pricing/{id}`, `/comps/{id}`, `/genie`.
- Lakebase connection via `generate_database_credential()` → psycopg (token refresh).
- Static tabs: Search · Map · Stay detail · Pricing Studio · Insights.
- 🔎 **Checkpoint:** app runs locally against Lakebase; search + price pills work.

## Phase 9 — Genie + Lakehouse Sync 🔎 (written; UI setup + verify)  → `notebooks/09_genie_and_sync.py`
**Goal:** analytics + the write-back loop.
- Genie space over gold; (optional) Revenue Copilot narration.
- Lakehouse Sync: `search_events`/`saved_properties` → UC (SCD2) for analytics.
- 🔎 **Checkpoint:** a save in the app appears in the UC synced table.

## Phase 10 — Deploy + demo 🔎 (bundle written + validated; deploy when ready)  → `databricks.yml`
> `databricks bundle validate` passes. Deploy steps + the critical service-principal grants are in
> [`DEPLOY.md`](./DEPLOY.md). The app also runs locally: `cd app && uvicorn server.main:app --port 8000`.
**Goal:** ship it and rehearse.
- Asset Bundle: pipeline + job + app. `databricks bundle deploy`.
- Rehearse the 3-min demo script from `PROJECT_BRIEF.md`.
- 🔎 **Checkpoint:** deployed app URL works; demo runs end to end.

---

## Priority if time runs short
Core (must): Phases 1–5, 7, 8. Then Reverse ETL (6). Genie/Sync (9) and Map tab are polish.
Cut order (last-in-first-out): holidays enrichment → Map tab → Revenue Copilot → Lakehouse Sync.

## After the core works → innovation backlog
Novel Lakebase differentiators are parked in [`ENHANCEMENTS.md`](./ENHANCEMENTS.md):
branch-based pricing sandboxes (E1), semantic comp sets (E2), time-travel demand replay (E3).
Do NOT start these until the core app runs end to end. Verify their dependencies in the Phase-4 smoke test.
