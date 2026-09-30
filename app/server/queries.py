"""
PriceWise query layer — the SQL behind each API endpoint.
Keeps all Lakebase SQL in one place: hybrid search, pricing serving, comps, detail.
"""
from . import db

RRF_K = 60          # RRF damping constant (lower = rank position matters more)
LIST_LIMIT = 80     # candidates pulled from each ranker before fusion

# The wanderbricks dataset has no real property names (title is generic like "Villa in Phuket").
# We synthesize a friendly, STABLE name from the property_id using word-bank arrays — deterministic,
# so the same listing always shows the same name. Used as `display_name` in card titles.
# NOTE: %% because psycopg treats a single % as a parameter marker; %% = literal modulo.
DISPLAY_NAME_SQL = """
  ((ARRAY['Azure','Golden','Serene','Coastal','Hidden','Sunlit','Palm','Ocean','Bella','Casa',
          'Marina','Lagoon','Sunset','Terra','Amara','Vista','Breeze','Coral','Zephyr','Laguna'])
     [(p.property_id %% 20) + 1]
   || ' ' ||
   (ARRAY['Retreat','Haven','Escape','Nest','Sands','Shores','Hideaway','Cove','Terrace','Garden',
          'Loft','House','Villa','Suites','Bungalow','Residence','Quarters','Lodge','Palms','Bay'])
     [((p.property_id / 7) %% 20) + 1])
"""


def hybrid_search(q: str, month: int, *, semantic: bool = True,
                  max_price: float | None = None, min_guests: int | None = None,
                  destination: str | None = None, limit: int = 20) -> list[dict]:
    """
    Hybrid search: vector (semantic) + BM25 (keyword), fused with RRF, joined to the
    live suggested price for `month`. Structured filters narrow both rankers.
    When semantic=False, returns keyword-only (the demo's "Semantic toggle off").
    """
    # destination narrows the candidate pool inside the CTEs (bare = no alias, p = aliased outer).
    dest_bare = " AND destination = %(destination)s" if destination else ""
    dest_p    = " AND p.destination = %(destination)s" if destination else ""
    # max_price filters on the SUGGESTED (displayed) price — applied in the outer query where
    # property_pricing (pr) is joined, so what the user types matches what they see on the card.
    price_p   = " AND pr.suggested_price <= %(max_price)s" if max_price is not None else ""
    filt_bare = dest_bare
    filt_p    = dest_p + price_p

    qvec = db.embed(q) if semantic else None

    # Use NAMED placeholders (%(name)s) so parameter order can't get mismatched — the same
    # name can appear multiple times in the SQL and psycopg binds it correctly everywhere.
    named = {"q": q, "qvec": qvec, "month": month, "k": RRF_K}
    if max_price is not None:
        named["max_price"] = max_price
    if destination:
        named["destination"] = destination

    if semantic:
        sql = f"""
        WITH vector_ranked AS (
          SELECT property_id, RANK() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS rank
          FROM properties
          WHERE TRUE {filt_bare}
          ORDER BY embedding <=> %(qvec)s::vector LIMIT {LIST_LIMIT}
        ),
        keyword_ranked AS (
          SELECT property_id,
                 RANK() OVER (ORDER BY body_tsv <@> to_bm25query(to_tsvector('english', %(q)s), 'idx_prop_bm25')) AS rank
          FROM properties
          WHERE TRUE {filt_bare}
          ORDER BY body_tsv <@> to_bm25query(to_tsvector('english', %(q)s), 'idx_prop_bm25') LIMIT {LIST_LIMIT}
        )
        SELECT p.property_id, p.title, {DISPLAY_NAME_SQL} AS display_name,
               p.property_type, p.destination, p.country,
               p.base_price, pr.suggested_price, pr.season_uplift, pr.comp_uplift,
               pr.fx_uplift, pr.holiday_uplift, pr.demand_pct,
               COALESCE(1.0/(%(k)s+v.rank),0) AS vec_rrf,
               COALESCE(1.0/(%(k)s+k.rank),0) AS kw_rrf,
               COALESCE(1.0/(%(k)s+v.rank),0)+COALESCE(1.0/(%(k)s+k.rank),0) AS score,
               (v.property_id IS NOT NULL) AS in_vector,
               (k.property_id IS NOT NULL) AS in_keyword
        FROM properties p
        LEFT JOIN vector_ranked  v ON v.property_id = p.property_id
        LEFT JOIN keyword_ranked k ON k.property_id = p.property_id
        LEFT JOIN property_pricing pr ON pr.property_id = p.property_id AND pr.target_month = %(month)s
        WHERE (v.property_id IS NOT NULL OR k.property_id IS NOT NULL) {filt_p}
        ORDER BY score DESC, p.property_id
        LIMIT {limit}
        """
    else:
        sql = f"""
        SELECT p.property_id, p.title, {DISPLAY_NAME_SQL} AS display_name,
               p.property_type, p.destination, p.country,
               p.base_price, pr.suggested_price, pr.season_uplift, pr.comp_uplift,
               pr.fx_uplift, pr.holiday_uplift, pr.demand_pct,
               0.0 AS vec_rrf, 1.0 AS kw_rrf, 1.0 AS score,
               FALSE AS in_vector, TRUE AS in_keyword
        FROM properties p
        LEFT JOIN property_pricing pr ON pr.property_id = p.property_id AND pr.target_month = %(month)s
        WHERE body_tsv @@ websearch_to_tsquery('english', %(q)s) {filt_p}
        ORDER BY body_tsv <@> to_bm25query(to_tsvector('english', %(q)s), 'idx_prop_bm25')
        LIMIT {limit}
        """

    return db.query(sql, named)


def pricing_for(property_id: int, month: int) -> dict | None:
    rows = db.query("""
        SELECT pr.*, p.title, p.destination, p.country, p.property_type
        FROM property_pricing pr JOIN properties p ON p.property_id = pr.property_id
        WHERE pr.property_id = %s AND pr.target_month = %s
    """, (property_id, month))
    return rows[0] if rows else None


def price_curve(property_id: int) -> list[dict]:
    """All 12 months for one property — the dynamic-pricing line chart."""
    return db.query("""
        SELECT target_month, base_price, suggested_price, season_index,
               season_uplift, comp_uplift, fx_uplift, holiday_uplift
        FROM property_pricing WHERE property_id = %s ORDER BY target_month
    """, (property_id,))


def property_detail(property_id: int) -> dict | None:
    rows = db.query("""
        SELECT property_id, title, property_type, destination, country, amenities,
               base_price, lat, lon, search_text
        FROM properties WHERE property_id = %s
    """, (property_id,))
    return rows[0] if rows else None


def comps_within(property_id: int, radius_mi: float = 20, month: int = 7,
                 limit: int = 8) -> list[dict]:
    """
    Geo comp set: comparable stays within `radius_mi` of this property (PostGIS),
    with their suggested price for `month`. (Semantic-similarity comps = ENHANCEMENTS E2.)
    """
    meters = radius_mi * 1609.34
    # Use explicit CROSS JOIN (not comma-join) so the LEFT JOIN can reference p.
    return db.query("""
        WITH me AS (SELECT geo FROM properties WHERE property_id = %(pid)s)
        SELECT p.property_id, p.title, p.destination, p.base_price,
               pr.suggested_price,
               ROUND((ST_Distance(p.geo, me.geo)/1609.34)::numeric, 1) AS miles
        FROM properties p
        CROSS JOIN me
        LEFT JOIN property_pricing pr
               ON pr.property_id = p.property_id AND pr.target_month = %(month)s
        WHERE p.property_id <> %(pid)s
          AND p.geo IS NOT NULL
          AND me.geo IS NOT NULL
          AND ST_DWithin(p.geo, me.geo, %(meters)s)
        ORDER BY miles
        LIMIT %(limit)s
    """, {"pid": property_id, "month": month, "meters": meters, "limit": limit})


def destinations() -> list[dict]:
    return db.query("""
        SELECT destination, count(*) AS n FROM properties
        WHERE destination IS NOT NULL GROUP BY destination ORDER BY n DESC
    """)


def save_property(user_id: str, property_id: int) -> None:
    db.execute("INSERT INTO saved_properties (user_id, property_id) VALUES (%s, %s)",
               (user_id, property_id))


def market_summary(month: int) -> dict:
    rows = db.query("""
        SELECT count(*) AS listings,
               ROUND(AVG(suggested_price - base_price)::numeric, 2) AS avg_uplift,
               ROUND((100*AVG((suggested_price-base_price)/NULLIF(base_price,0)))::numeric,1) AS avg_uplift_pct
        FROM property_pricing WHERE target_month = %s
    """, (month,))
    return rows[0] if rows else {}
