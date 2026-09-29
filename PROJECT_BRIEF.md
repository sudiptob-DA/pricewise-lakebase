# PriceWise — Project Brief (Draft)

_Databricks Lakebase Hackathon (Sept 2026), Track 2 (Data & ML engineers)._
_Formerly drafted as "Perch." Built on `samples.wanderbricks` (+ optional light holidays enrichment)._

## One-liner
**PriceWise** is the demand-aware pricing brain and smart search behind a leisure-stay marketplace. It spots when a listing is under-priced for the season and neighborhood, serves a **real-time suggested price** from Lakebase, and lets guests find stays by describing them in plain language. Hosts capture peak-demand revenue they're currently leaving on the table.

**The wedge (proven by the data):** in this marketplace July books ~10× January, but the average nightly price is flat all year. That gap is money on the table — PriceWise closes it.

## Why this idea (grounded in real data profiling)
We profiled the data before committing. Findings that shaped the concept:
- **Strong, real seasonality:** 72,247 bookings over 2.5 yrs — July 27,851 / June 17,970 vs January 2,388 / October 1,258 — yet avg nightly amount is flat (~$550 every month). **Under-pricing peak demand is provable from their own data.**
- **Descriptions are the semantic corpus, not reviews:** 18,138 distinct property descriptions (+ rich editorial destination guides). Reviews are templated — 99,317 rows but only **15 distinct comments** — so reviews feed the numeric `rating` signal only, never semantic search.
- **Weather was dropped from the core:** AccuWeather here is 50 global cities, July-only, static; only 9 of 42 destinations name-match and none of the marquee beach/ski spots. Kept out of the pricing engine to avoid a hollow demo (optional tiny "conditions" chip on the 9 city destinations at most).
- **Geo is real and global:** property lat/long supports 20-mile comp sets and "similar nearby" — meaningful inside dense destinations (Phuket has 1,788 stays).

## Business case (rubric: 20%)
- **Buyer / user:** hosts and the marketplace's revenue-management team (pricing); guests (search).
- **Replaces:** static, manually-set nightly prices that ignore season, destination demand, and neighborhood comps — plus blunt keyword filters.
- **Why it matters:** demand-aware pricing directly lifts revenue per available night; better search lifts conversion. Both are direct GMV levers, and the upside is quantified from the marketplace's own booking history.

## The data (core relational source: `samples.wanderbricks` + 2 external enrichments)
- **18,163 properties** across **42 global destinations**, $56–$562/night (avg $182).
- **Property types:** Urban Year-Round 9,264 · Summer Getaway 7,068 · Historical 1,646 · Ski Resort 185.
- **Top destinations:** Phuket 1,788 · Mallorca 1,626 · Gold Coast 1,624 · Paris 1,440 · Abu Dhabi 1,103 · Tokyo 918 · Dubai 787 · Osaka · Singapore · New York …
- **Tables used:** `properties` (title, description, base_price, type, beds/baths, lat/long, destination_id), `reviews` (rating), `bookings` (check_in/out, total_amount, status → seasonality/occupancy/revenue), `clickstream` (per-property popularity — 100k events, 1-day snapshot), `destinations` (name/state/country + editorial guide), `amenities`/`property_amenities`, `hosts`, `users`.
### External enrichment sources (verified live, free, no-auth)
Both are tiny one-time ingests cached into Delta tables — **never on the request path**, so nothing breaks in the demo.
| Source | API | Join | Signal | Notes |
|---|---|---|---|---|
| **FX rates** (primary) | `frankfurter.dev` (ECB-backed) | country → currency → rate (+ historical) | **cross-border demand** — a weakening destination currency makes it cheaper for foreign guests → demand shifts | Complete coverage (every country has a currency); current + historical both verified |
| **Public holidays** (secondary) | `date.nager.at` | `destinations.country` + date | **holiday/school-break demand** (e.g., Golden Week → Tokyo, CNY → Singapore) | 15/18 countries covered; **gaps: Thailand, UAE, India**. Apply boost where data exists; small manual backfill for Thailand's Songkran (Phuket is #1 market) |

**FX angle (novel):** "JPY weakened ~3% vs. USD → Tokyo is cheaper for US guests → international demand up → nudge price." Few teams think of currency as a pricing signal.

## Architecture — two data planes

### Operational layer → Lakebase (Postgres)
- **`properties`** — searchable catalog with `lakebase_vector` embeddings (title + description + destination-guide context) and `lakebase_text` BM25 (title, description, amenity names); `geography` column for PostGIS radius queries.
- **`property_pricing`** — the **real-time serving table**: `suggested_price`, `demand_score`, `season_factor`, `comp_percentile`, `holiday_boost`, `fx_factor`, `occupancy_30d` per property. Point-lookup by `property_id` in <200ms.
- **`search_events` / `saved_properties`** — stateful user actions (a save = a row insert), written back to the lakehouse via Lakehouse Sync.

### Analytics layer → Genie over lakehouse gold
Natural-language BI (off the request path): revenue & occupancy by destination, seasonality curves, price-vs-demand gap, "which listings are most under-priced." Optional **Revenue Copilot** narrates pricing recommendations.

## The three hero features

### 1. Demand-aware dynamic pricing (Feature Store — "real-time serving is the goal")
- **Compute in lakehouse (gold):** `season_factor` from booking seasonality per destination/month; `demand_score` from clickstream popularity + booking pace; `comp_percentile` from the 20-mi comp set; `holiday_boost` from Nager.Date (where covered); `fx_factor` from frankfurter.dev (destination currency strength vs. guest currency).
- **`suggested_price = base_price × f(season_factor, demand_score, comp_percentile, holiday_boost, fx_factor)`.**
- **Publish to the Online Feature Store on Lakebase**, served by `property_id` in ~200ms. Each card and the Pricing Studio render it live — not recomputed inline. *That lookup is the real-time feature serving.*

### 2. Hybrid search (Lakebase Search) — the "wow" beat
One SQL statement fuses semantic + keyword + structured filters:
```sql
SELECT p.property_id, p.title, p.suggested_price
FROM properties p
WHERE p.max_guests >= :guests AND p.base_price <= :max_price
  AND p.destination_id = :dest            -- optional
ORDER BY rrf(
  p.embedding <=> :query_vec,             -- lakebase_vector (semantic, over descriptions)
  p.search_text <@> bm25(:query_text)     -- lakebase_text (BM25 keyword)
) LIMIT 20;
```
**Demo beat:** search *"romantic sea-view escape with nightlife."* Top results are Santorini/Mykonos listings whose **descriptions** convey it though the word "romantic" never appears. Toggle Semantic off → they drop out. Vector found what keyword missed. (Grounded in the 18,138 distinct real descriptions.)

### 3. Geo comps & similar-nearby (Lakebase = vector + geo + SQL in one system)
- **20-mile comp set:** PostGIS `ST_DWithin` radius + `lakebase_vector` similarity → "comparable stays within 20 mi and how they're priced," driving `comp_percentile`.
- **"Similar stays nearby":** the same fusion powers guest-side "you may also like."

## Multi-tab app
- **Search** (guest) — hybrid search + Semantic toggle + demand-aware price pills.
- **Map** (guest, stretch) — pins with price; draw a radius ("within 20 mi"). Leaflet + free OSM tiles (no token).
- **Stay detail** (guest) — gallery, description, rating, "why this price," similar-within-20-mi.
- **Pricing Studio** (host) — **the centerpiece:** seasonality "money left on the table" chart, demand drivers, 20-mi comp set, real-time suggested price with Accept/Override.
- **Insights** (both) — Genie analytics + optional Revenue Copilot.

## Pipeline shape
`samples.wanderbricks` + FX (frankfurter.dev) + holidays (Nager.Date) → **silver → gold** (Lakeflow Declarative Pipeline: property doc, seasonality/demand/comp/holiday/fx features, geo geography) → **embeddings** (Model Serving, e.g. `databricks-gte`/`bge`) + **features** (Feature Engineering SDK) → **Reverse ETL / Synced Tables** → **Lakebase** (`properties`, `property_pricing`) → **Databricks App**. App writes (searches, saves) flow back via **Lakehouse Sync** for analytics + retraining. Cadence: triggered/daily refresh (a "refresh" button stands in for the schedule in the demo).

## Databricks breadth (rubric: 15%)
Lakebase (Search + online store + OLTP state + PostGIS) · Lakeflow Declarative Pipelines · Unity Catalog · Model Serving (embeddings) · Feature Store · Genie (+ optional Revenue Copilot) · Databricks Apps · Asset Bundles.

## The app (build)
- **Framework:** Databricks App — **FastAPI + polished static HTML/JS** (fast, pixel control). Streamlit fallback.
- **Lakebase connection:** short-lived credential via SDK `generate_database_credential()` → `psycopg`, ~1 hr refresh; app principal / PAT gets a Postgres role.

## What's real vs mocked (hackathon)
- **Real:** hybrid SQL on Lakebase, embeddings via Model Serving, demand/season/comp features served from the online store, geo comps, seasonality analytics, Genie panel, the app.
- **Simplified:** triggered/daily refresh (button) stands in for a live feed; pricing is a transparent, explainable heuristic (season + demand + comps [+ holiday]) rather than a trained ML model — swappable later. Clickstream is a 1-day popularity snapshot (used as relative demand, not a trend). Revenue Copilot + Map are stretch.
- **To verify early:** `lakebase_text` (BM25), `lakebase_vector`, and PostGIS extensions on the instance; Online Feature Store publish path to Lakebase.

## Rubric fit (Track 2)
- **Creativity 30%** — Lakebase Search (hybrid, over real descriptions) + real-time price serving + vector-plus-geo comps in one system; a data-proven pricing insight, not a generic clone.
- **Business 20%** — pricing + search = direct GMV levers; upside quantified from the marketplace's own bookings.
- **Demo 20%** — the seasonality "money on the table" chart + live suggested price + the semantic toggle.
- **Breadth 15%** — see above.
- **UX 15%** — multi-tab app, live price pills, comp set, "why this price" explainer.

## Demo script (~3 min)
1. **Pricing Studio:** open a Phuket villa → seasonality chart shows July 10× January at a flat price → "~$9.4k/yr uncaptured." Suggested price $268 vs base $205 (served <200ms). 20-mi comps show it's in the bottom 25%. Accept.
2. **Search:** "romantic sea-view escape with nightlife" → semantic surfaces Santorini/Mykonos; toggle Semantic off → they vanish (wow beat).
3. Price pills on results update live from the online store.
4. **Insights (Genie):** "revenue by destination," "most under-priced listings"; Revenue Copilot narrates.
5. Save a stay → Lakehouse Sync writes it back → analytics reflects it.

## Status / next steps
- [x] Idea + name locked; data profiled live (seasonality, descriptions-vs-reviews, geo, weather ruled out of core).
- [x] Workspace wired: auth (`.databrickscfg`), Databricks CLI, warehouse, team schema.
- [ ] Provision Lakebase instance (`pricewise-db`, smallest capacity).
- [ ] Gold pipeline: property doc; seasonality/demand/comp features; geo geography column.
- [ ] Embeddings (Model Serving) + `lakebase_vector` / `lakebase_text` / PostGIS indexes.
- [ ] Reverse ETL / Synced Tables → Lakebase (`properties`, `property_pricing`).
- [ ] Hybrid search + 20-mi comp queries; FastAPI endpoints; static multi-tab UI.
- [ ] Online Feature Store publish + serve path.
- [ ] Genie space over gold; (optional) Revenue Copilot, Map tab, holidays enrichment; Lakehouse Sync write-backs.
- [ ] `app.yaml` + Asset Bundle for Databricks Apps deploy.

## Workspace facts (for build)
- Catalog/schema (team): `hackathon.data_axle` · Sources: `samples.wanderbricks` + FX (`frankfurter.dev`) + holidays (`date.nager.at`)
- SQL warehouse: `hackathon-shared-small` (id `64d934278b5c472d`)
- Workspace host: `dbc-9d25a17d-f58c.cloud.databricks.com` (org `7474651394607811`, dnb-hackathon-west-1)
