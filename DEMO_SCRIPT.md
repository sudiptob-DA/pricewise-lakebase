# PriceWise — Demo Script (~3 minutes)

**The one idea:** *A host prices their listing flat while demand swings wildly — so they leave money
on the table every peak season. PriceWise is the pricing brain that fixes that.*

**Who's the customer:** the **host / seller** who lists on Expedia, Airbnb, Booking.com.
**Who they're trying to reach:** guests. (So search is *their* tool to see how they get found.)

> Tip: before recording, run one search to warm the Lakebase compute (avoids a cold-start pause).
> Keep the browser at http://127.0.0.1:8000, month = July.

---

## ① Hook — the problem (~20s)
*(App open on Market Search)*

> "Meet a host with a villa in Phuket. They list on the big travel sites, set one nightly price, and
> rarely change it. But here's the problem: in July they get **ten times** the demand of January — at
> the **same price**. Every peak night, they're leaving money on the table. PriceWise fixes that."

## ② Smart search — how they see their market (~40s)
*(Market Search tab)*

> "First, PriceWise understands listings the way a guest actually thinks. I'll search
> **'romantic sea-view escape with a pool and nightlife.'**"

*(Results appear. Now toggle **Smart understanding** OFF.)*

> "With plain keyword matching, these results vanish — because the listings never literally say
> 'romantic.' PriceWise understands **meaning**, not just words. That's how a host makes sure the
> right guests find them — and how they see who they're competing with."

*(Toggle it back ON.)*

## ③ The pricing brain — the core (~70s)
*(Click a Phuket villa → Pricing Studio)*

> "Now the heart of it. Here's the villa — and PriceWise recommends a nightly price, and **explains
> exactly why.**"

*(Point at the waterfall, top to bottom.)*
> "We start with the host's base price. We add for **peak-season demand** — July is their busiest
> month. We adjust for **local competition** — how comparable villas nearby are priced. We factor in
> **holiday demand**. And even **currency** — most guests here are international, so when the local
> currency weakens, this villa gets cheaper for them and demand rises."

*(Point at the 12-month chart.)*
> "And it's not one number — it's **every month**. Low in the off-season, premium at the peak.
> Dynamic pricing the host could never work out by hand."

*(Point at the 20-mile comps.)*
> "It even shows comparable stays **within 20 miles**, so the price is grounded in the real neighborhood."

## ④ The payoff — the business case (~25s)
*(Revenue Insights tab)*

> "Across all their listings, that's real money recovered every peak season. For a host, **one extra
> well-priced month can pay for this tool many times over** — and they never have to touch a
> spreadsheet."

## ⑤ The 'how' — for the judges (~20s, optional)

> "And it all runs on **one system — Databricks Lakebase**. The search, the pricing, and the live data
> serving all come from a single Postgres database that updates instantly — no separate search engine,
> no glue pipelines. Search, geo, and real-time pricing in one place."

---

## If asked "aren't you just competing with Expedia?"
> "No — we're not a booking site. We **arm the host** who lists on Expedia with the pricing
> intelligence the big chains have and small hosts can't afford. We democratize revenue management."

## Rubric mapping (say these words if you can)
- **Creativity (30%)** — hybrid search + real-time, explainable pricing on one Lakebase; weather-free,
  data-proven demand model.
- **Business (20%)** — hosts under-price peak demand; provable from their own bookings (10× swing, flat price).
- **Demo (20%)** — the Smart-understanding toggle + the price waterfall + the 12-month curve.
- **Breadth (15%)** — Lakebase (Search + serving + OLTP), Model Serving (embeddings), PostGIS, Genie (coming), Apps.
- **UX (15%)** — clean multi-tab app, plain-English pricing explanation, live prices.

## Safety / honesty notes (don't overclaim)
- Lead with **season + local competition** (big, intuitive levers). Present **currency & holidays** as
  smart extras, not headliners — their dollar impact is smaller.
- The demand model is a **transparent heuristic** (season + demand + comps + holiday + FX), not a black box.
  That's a feature: hosts trust a price they can see explained.
