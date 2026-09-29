# PriceWise — Enhancements Backlog

Innovation ideas that make PriceWise a **novel Lakebase use case**, not just another pricing tool.
**Deliberately deferred** — build the core app first (see `BUILD_GUIDE.md` Phases 1–10). Pick these up
once the core works, in priority order below.

> Guiding principle: each of these leans on a Lakebase capability that competing stacks (warehouse +
> separate Postgres + separate vector DB + feature store + glue ETL) can't do cleanly. That's the
> "why Lakebase" story judges reward.

---

## ⭐ E1 — Pricing Sandboxes on Lakebase Branching (headline differentiator)
**One-liner:** *Git branches, but for pricing strategy — test a year of pricing against real demand in
seconds, with zero risk to live inventory.*

**The problem:** revenue managers won't experiment with pricing on live inventory (a bad rule in peak
season costs real money), so they under-price — the exact gap PriceWise exists to close.

**The idea:**
1. Spin up an **instant, zero-copy branch** of the live pricing DB.
2. Apply a candidate strategy on the branch (e.g. "+40% beachfront in July", "discount rainy-forecast
   lake stays", "aggressive FX pricing for US guests").
3. **Simulate against real historical bookings** on the branch → projected revenue vs. current strategy.
4. **Promote** the branch if it wins, **discard** if not. Production is never touched.

**Why novel:** no consumer pricing tool (Airbnb Smart Pricing, IDeaS, Duetto) forks the live DB to A/B a
strategy on real data instantly — a normal Postgres/warehouse copy is slow + costly. Lakebase zero-copy
branching makes it free + instant. Most hackathon teams use branching for dev/test; using it as a
**product feature for pricing experimentation** is the fresh interpretation (Track 2 creativity, 30%).

**Lakebase features used:** branching (zero-copy), + reuses hybrid search, real-time serving, pricing waterfall.

**Demo beat:** in Pricing Studio → "Test strategy" → branch → apply rule → simulate on real bookings →
"+$X projected" → Promote. One compelling experiment, not a whole platform.

**Effort:** M–L. **Risk:** ⚠️ must verify Lakebase branching is API/CLI/SDK-accessible on the hackathon
instance (added to Phase-4 smoke test). If gated → fall back to E3 (snapshot replay).

**Acceptance:** create branch → apply pricing rule → simulate → show revenue delta → promote/discard, live.

---

## E2 — Semantic Comp Sets (subtle, high-value, low-risk)
**One-liner:** *Price a listing like its semantic twins nearby — comps defined by embedding similarity +
geo radius, not crude category buckets.*

**The idea:** the pricing comp set (which drives `comp_percentile`) is built from **vector similarity of
descriptions** (`lakebase_vector`) **intersected with a geo radius** (PostGIS `ST_DWithin`, ~20 mi),
instead of "same property_type in same destination." So a "boho beachfront villa with sea views" is
compared to genuinely similar stays, not just any 3-bed in the city.

**Why novel:** embeddings are normally used for *search*; using them to define *pricing comparables* is an
original twist — and it's a single Lakebase query (vector + geo + SQL) that a multi-system stack couldn't
do in one shot.

**Lakebase features used:** `lakebase_vector` + PostGIS geo + SQL, one query.

**Effort:** S–M (mostly extends the comp query we're already building). **Risk:** low.
**Note:** this one is close to the core — could be folded into the main comp-set implementation rather than
deferred, if time allows. Keep here as the "upgrade" version of comps.

**Acceptance:** comp set for a listing returns semantically-similar nearby stays; `comp_percentile`
computed from them; visibly better comps than category-only.

---

## E3 — Time-Travel Demand Replay (powerful demo beat, good fallback for E1)
**One-liner:** *Replay last summer and show what PriceWise would have charged vs. what actually happened
— "we'd have recovered $X."*

**The idea:** use a Lakebase **snapshot/branch** (or lakehouse time-travel on gold) to reconstruct a past
peak period, run the PriceWise engine over it, and compare **suggested price × realized occupancy** to the
**actual flat price × occupancy**. Quantifies value on *real history*, not hypotheticals.

**Why novel / valuable:** turns the abstract "you're leaving money on the table" into a concrete, backed
number over a real season — the single most persuasive slide for the business case (20%).

**Lakebase features used:** snapshots / branching / Delta time-travel.

**Effort:** M. **Risk:** low-medium (depends on snapshot or time-travel access). Doubles as the fallback
"what-if on history" story if E1 branching is gated.

**Acceptance:** pick a past summer → engine prices it → show recovered-revenue delta with a chart.

---

## Suggested order (after core app is done + rehearsed)
1. **E2** first — cheapest, closest to core, upgrades comps immediately.
2. **E3** next — high-persuasion demo beat, modest effort, low risk.
3. **E1** last — the headline wow, but highest effort + the branching-availability risk. Only if time and
   the Phase-4 smoke test confirm branching works.

## Dependencies / to verify (Phase-4 smoke test)
- [ ] Lakebase **branching** API accessible (create branch, connect, promote/discard)? → gates E1.
- [ ] **Snapshots** or Delta **time-travel** usable for replay? → gates E3.
- [ ] `lakebase_vector` + PostGIS confirmed (already on the list) → gates E2.
