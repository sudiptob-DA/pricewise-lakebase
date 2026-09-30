"""
PriceWise query layer — the SQL behind each API endpoint.
Keeps all Lakebase SQL in one place: hybrid search, pricing serving, comps, detail.
"""
from . import db

RRF_K = 60          # RRF damping constant (lower = rank position matters more)
LIST_LIMIT = 80     # candidates pulled from each ranker before fusion


def hybrid_search(q: str, month: int, *, semantic: bool = True,
                  max_price: float | None = None, min_guests: int | None = None,
                  destination: str | None = None, limit: int = 20) -> list[dict]:
    """
    Hybrid search: vector (semantic) + BM25 (keyword), fused with RRF, joined to the
    live suggested price for `month`. Structured filters narrow both rankers.
    When semantic=False, returns keyword-only (the demo's "Semantic toggle off").
    """
    filt, params_filter = [], []
    if max_price is not None:
        filt.append("p.base_price <= %s"); params_filter.append(max_price)
    if destination:
        filt.append("p.destination = %s"); params_filter.append(destination)
    where_filter = (" AND " + " AND ".join(filt)) if filt else ""

    qvec = db.embed(q) if semantic else None

    if semantic:
        sql = f"""
        WITH vector_ranked AS (
          SELECT property_id, RANK() OVER (ORDER BY embedding <=> %s::vector) AS rank
          FROM properties
          WHERE TRUE {where_filter}
          ORDER BY embedding <=> %s::vector LIMIT {LIST_LIMIT}
        ),
        keyword_ranked AS (
          SELECT property_id,
                 RANK() OVER (ORDER BY body_tsv <@> to_bm25query(to_tsvector('english', %s), 'idx_prop_bm25')) AS rank
          FROM properties
          WHERE TRUE {where_filter}
          ORDER BY body_tsv <@> to_bm25query(to_tsvector('english', %s), 'idx_prop_bm25') LIMIT {LIST_LIMIT}
        )
        SELECT p.property_id, p.title, p.property_type, p.destination, p.country,
               p.base_price, pr.suggested_price, pr.season_uplift, pr.comp_uplift,
               pr.fx_uplift, pr.holiday_uplift, pr.demand_pct,
               COALESCE(1.0/(%s+v.rank),0) AS vec_rrf,
               COALESCE(1.0/(%s+k.rank),0) AS kw_rrf,
               COALESCE(1.0/(%s+v.rank),0)+COALESCE(1.0/(%s+k.rank),0) AS score,
               (v.property_id IS NOT NULL) AS in_vector,
               (k.property_id IS NOT NULL) AS in_keyword
        FROM properties p
        LEFT JOIN vector_ranked  v ON v.property_id = p.property_id
        LEFT JOIN keyword_ranked k ON k.property_id = p.property_id
        LEFT JOIN property_pricing pr ON pr.property_id = p.property_id AND pr.target_month = %s
        WHERE (v.property_id IS NOT NULL OR k.property_id IS NOT NULL) {where_filter}
        ORDER BY score DESC, p.property_id
        LIMIT {limit}
        """
        # params MUST match placeholder order in the SQL above:
        #   vector CTE: qvec, qvec, [filter]
        #   keyword CTE: q, q, [filter]
        #   SELECT rrf constants: K, K, K, K
        #   pricing join: month
        #   outer WHERE: [filter]
        params = ([qvec, qvec] + params_filter
                  + [q, q] + params_filter
                  + [RRF_K, RRF_K, RRF_K, RRF_K]
                  + [month]
                  + params_filter)
    else:
        sql = f"""
        SELECT p.property_id, p.title, p.property_type, p.destination, p.country,
               p.base_price, pr.suggested_price, pr.season_uplift, pr.comp_uplift,
               pr.fx_uplift, pr.holiday_uplift, pr.demand_pct,
               0.0 AS vec_rrf, 1.0 AS kw_rrf, 1.0 AS score,
               FALSE AS in_vector, TRUE AS in_keyword
        FROM properties p
        LEFT JOIN property_pricing pr ON pr.property_id = p.property_id AND pr.target_month = %s
        WHERE body_tsv @@ websearch_to_tsquery('english', %s) {where_filter}
        ORDER BY body_tsv <@> to_bm25query(to_tsvector('english', %s), 'idx_prop_bm25')
        LIMIT {limit}
        """
        params = [month, q] + params_filter + [q]

    return db.query(sql, tuple(params))


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
    return db.query("""
        WITH me AS (SELECT geo FROM properties WHERE property_id = %s)
        SELECT p.property_id, p.title, p.destination, p.base_price,
               pr.suggested_price,
               ROUND((ST_Distance(p.geo, me.geo)/1609.34)::numeric, 1) AS miles
        FROM properties p, me
        LEFT JOIN property_pricing pr ON pr.property_id = p.property_id AND pr.target_month = %s
        WHERE p.property_id <> %s
          AND p.geo IS NOT NULL
          AND ST_DWithin(p.geo, me.geo, %s)
        ORDER BY miles
        LIMIT %s
    """, (property_id, month, property_id, meters, limit))


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
