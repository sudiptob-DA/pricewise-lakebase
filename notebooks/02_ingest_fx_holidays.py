# Databricks notebook source
# MAGIC %md
# MAGIC # PriceWise · 02 · Ingest external signals (FX + Holidays)
# MAGIC
# MAGIC **Goal:** pull two free, no-auth public sources *once* and land them as Delta tables in
# MAGIC `hackathon.data_axle`. These enrich our pricing with real-world demand signals:
# MAGIC - **FX rates** (`frankfurter.dev`, ECB-backed) → cross-border demand: a weak destination
# MAGIC   currency makes it cheaper for foreign guests.
# MAGIC - **Public holidays** (`date.nager.at`) → holiday / school-break demand.
# MAGIC
# MAGIC **Design rule:** we ingest to Delta *once* (cached). The app and pricing NEVER call these APIs
# MAGIC on the request path — so a flaky API can't break the demo.
# MAGIC
# MAGIC > Import into the workspace, attach to Serverless, Run All. Uses `requests` (available on
# MAGIC > Databricks runtime). If a call fails, re-run that cell — these are idempotent overwrites.

# COMMAND ----------
# MAGIC %md
# MAGIC ## 0. Setup + country mapping
# MAGIC We derive the country list from our own destinations, then map each to an ISO country code
# MAGIC (for holidays) and a currency code (for FX). Only these two dicts are hand-maintained.

# COMMAND ----------
import requests
from datetime import date, datetime

TARGET_CATALOG = "hackathon"
TARGET_SCHEMA = "data_axle"
SOURCE = "samples.wanderbricks"
spark.sql(f"USE CATALOG {TARGET_CATALOG}")
spark.sql(f"USE SCHEMA {TARGET_SCHEMA}")

# Country name (as it appears in destinations) -> (ISO2, currency)
COUNTRY_MAP = {
    "Thailand": ("TH", "THB"), "Spain": ("ES", "EUR"), "Australia": ("AU", "AUD"),
    "France": ("FR", "EUR"), "United Arab Emirates": ("AE", "AED"), "Japan": ("JP", "JPY"),
    "Singapore": ("SG", "SGD"), "United States": ("US", "USD"), "Germany": ("DE", "EUR"),
    "Italy": ("IT", "EUR"), "United Kingdom": ("GB", "GBP"), "Egypt": ("EG", "EGP"),
    "India": ("IN", "INR"), "Greece": ("GR", "EUR"), "Canada": ("CA", "CAD"),
    "Switzerland": ("CH", "CHF"), "Austria": ("AT", "EUR"), "China": ("CN", "CNY"),
}
BASE_CURRENCY = "USD"   # guest reference currency for the demo

# COMMAND ----------
# MAGIC %md
# MAGIC ### Which countries are actually in our data?
# MAGIC Sanity check: make sure every destination country is covered by `COUNTRY_MAP`.

# COMMAND ----------
countries_in_data = [r["country"] for r in
    spark.sql(f"SELECT DISTINCT country FROM {SOURCE}.destinations ORDER BY country").collect()]
missing = [c for c in countries_in_data if c not in COUNTRY_MAP]
print("Countries in data:", countries_in_data)
print("Unmapped (fix COUNTRY_MAP if any):", missing or "none ✅")

# COMMAND ----------
# MAGIC %md
# MAGIC ## 1. FX rates — current + 12-month history
# MAGIC `frankfurter.dev` gives ECB reference rates with `base=USD`. We pull the latest snapshot and
# MAGIC one point ~12 months ago, so we can show *currency movement* (the interesting signal), not
# MAGIC just a static number.

# COMMAND ----------
FX_BASE = "https://api.frankfurter.dev/v1"
currencies = sorted({cur for _, cur in COUNTRY_MAP.values()} - {BASE_CURRENCY})
symbols = ",".join(currencies)

def fx_on(day):
    """day='latest' or 'YYYY-MM-DD'. Returns {currency: rate_per_1_USD}."""
    url = f"{FX_BASE}/{day}?base={BASE_CURRENCY}&symbols={symbols}"
    r = requests.get(url, timeout=20); r.raise_for_status()
    j = r.json()
    return j["date"], j["rates"]

latest_date, latest = fx_on("latest")
year_ago = date(date.today().year - 1, date.today().month, min(date.today().day, 28)).isoformat()
hist_date, hist = fx_on(year_ago)
print("Latest FX date:", latest_date, "| history date:", hist_date)

fx_rows = []
for cur in currencies:
    now, then = latest.get(cur), hist.get(cur)
    # rate = units of foreign currency per 1 USD. If 'now' > 'then', foreign currency WEAKENED
    # vs USD -> destination cheaper for US guests -> demand tailwind.
    pct_change = round(100.0 * (now - then) / then, 2) if (now and then) else None
    fx_rows.append((cur, float(now) if now else None, float(then) if then else None,
                    pct_change, latest_date))
fx_rows.append((BASE_CURRENCY, 1.0, 1.0, 0.0, latest_date))  # base currency reference

# COMMAND ----------
from pyspark.sql import Row
fx_df = spark.createDataFrame([
    Row(currency=c, rate_per_usd=n, rate_year_ago=t, pct_change_yoy=p, as_of=d)
    for (c, n, t, p, d) in fx_rows
])
fx_df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable("fx_rates")
display(spark.sql("SELECT * FROM fx_rates ORDER BY pct_change_yoy"))

# COMMAND ----------
# MAGIC %md
# MAGIC **Read it:** a **positive `pct_change_yoy`** means that currency weakened vs. USD over the
# MAGIC year → the destination got cheaper for US guests → a demand tailwind we can price into.
# MAGIC (Later we translate this into a small, capped `fx_factor`.)

# COMMAND ----------
# MAGIC %md
# MAGIC ## 2. Public holidays — per destination country (current year)
# MAGIC `date.nager.at` returns public holidays for a country/year. Coverage is good but **not
# MAGIC universal** — Thailand, UAE, India are NOT supported. We fetch what we can and record which
# MAGIC countries are covered, so the pricing logic can apply a boost only where data exists.

# COMMAND ----------
NAGER = "https://date.nager.at/api/v3/PublicHolidays"
YEAR = date.today().year

holiday_rows = []
covered, uncovered = [], []
for country_name, (iso2, _cur) in COUNTRY_MAP.items():
    try:
        r = requests.get(f"{NAGER}/{YEAR}/{iso2}", timeout=20)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or not data:
            raise ValueError("empty")
        covered.append(iso2)
        for h in data:
            holiday_rows.append((iso2, country_name, h["date"], h.get("name"),
                                 h.get("localName"), bool(h.get("global", True))))
    except Exception as e:
        uncovered.append(iso2)
print("Covered:", covered)
print("NOT covered (expected TH/AE/IN):", uncovered)

# COMMAND ----------
# MAGIC %md
# MAGIC ### Manual backfill — Thailand's Songkran (Phuket is our #1 market)
# MAGIC Nager doesn't cover Thailand, but Songkran (Thai New Year, ~Apr 13–15) is a fixed, huge
# MAGIC demand window. We hardcode just this one so our top destination isn't blind.

# COMMAND ----------
for d in ("13", "14", "15"):
    holiday_rows.append(("TH", "Thailand", f"{YEAR}-04-{d}", "Songkran (Thai New Year)",
                         "สงกรานต์", True))

holidays_df = spark.createDataFrame([
    Row(country_code=cc, country=cn, holiday_date=hd, name=nm, local_name=ln, is_global=g)
    for (cc, cn, hd, nm, ln, g) in holiday_rows
])
holidays_df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable("holidays")
print("Total holiday rows:", holidays_df.count())
display(spark.sql("""
SELECT country, count(*) AS n_holidays
FROM holidays GROUP BY country ORDER BY n_holidays DESC
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ### Peek: upcoming holidays by country

# COMMAND ----------
display(spark.sql("""
SELECT country, holiday_date, name
FROM holidays
WHERE holiday_date >= current_date()
ORDER BY holiday_date LIMIT 25
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ## 3. Verify the joins work (this is the whole point)
# MAGIC Both enrichment tables must join cleanly to `destinations.country`. If these return rows,
# MAGIC notebook 03 can compute `fx_factor` and `holiday_boost` per property.

# COMMAND ----------
display(spark.sql(f"""
SELECT d.destination, d.country, fx.currency, fx.rate_per_usd, fx.pct_change_yoy
FROM {SOURCE}.destinations d
JOIN fx_rates fx
  ON fx.currency = CASE d.country
       WHEN 'Thailand' THEN 'THB' WHEN 'Japan' THEN 'JPY' WHEN 'Australia' THEN 'AUD'
       WHEN 'United Kingdom' THEN 'GBP' WHEN 'United States' THEN 'USD'
       WHEN 'Canada' THEN 'CAD' WHEN 'Switzerland' THEN 'CHF' WHEN 'China' THEN 'CNY'
       WHEN 'India' THEN 'INR' WHEN 'Egypt' THEN 'EGP' WHEN 'United Arab Emirates' THEN 'AED'
       WHEN 'Singapore' THEN 'SGD' ELSE 'EUR' END
ORDER BY fx.pct_change_yoy LIMIT 15
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC > Note: that big `CASE` is just to demo the join inline. In notebook 03 we'll add a small
# MAGIC > `country → currency` mapping table so the join is clean and reusable.

# COMMAND ----------
display(spark.sql(f"""
SELECT d.country, count(DISTINCT h.holiday_date) AS holidays_this_year
FROM {SOURCE}.destinations d
LEFT JOIN holidays h ON h.country = d.country
GROUP BY d.country ORDER BY holidays_this_year DESC
"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ## ✅ Checkpoint
# MAGIC You now have two governed Delta tables in `hackathon.data_axle`:
# MAGIC - **`fx_rates`** — currency, rate vs USD, YoY % change (the cross-border signal).
# MAGIC - **`holidays`** — public holidays per covered country (+ Songkran backfill for Thailand).
# MAGIC
# MAGIC Both join to `destinations.country`. Coverage gaps (TH/AE/IN for holidays) are known and
# MAGIC handled — pricing applies a holiday boost only where data exists.
# MAGIC
# MAGIC **Next:** `03_gold_features.py` — combine wanderbricks + FX + holidays into the gold feature
# MAGIC tables (`gold_property_doc`, `gold_property_features`) that power search and pricing.
