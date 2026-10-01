"""
PriceWise API — FastAPI backend for the Databricks App.

Routes:
  /api/health              liveness
  /api/destinations        list destinations (filter dropdown)
  /api/search              hybrid search (semantic toggle, filters), returns priced results
  /api/property/{id}       property detail
  /api/pricing/{id}        pricing waterfall for a month
  /api/pricing/{id}/curve  12-month price curve
  /api/comps/{id}          geo comps within radius
  /api/market              marketplace uplift summary
  /api/save                save a property (OLTP write)
  /                        serves the static UI
"""
import os
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import queries

app = FastAPI(title="PriceWise", version="0.1.0")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")


@app.get("/api/health")
def health():
    return {"status": "ok", "pricing_source": queries.pricing_source()}


@app.get("/api/config")
def config():
    """Front-end config. Set GENIE_SPACE_URL (full https URL to the Genie space) in the env;
    the Insights tab embeds it and uses it for the 'Open in Genie' button."""
    return {"genie_space_url": os.environ.get("GENIE_SPACE_URL", "")}


@app.get("/api/destinations")
def get_destinations():
    return queries.destinations()


@app.get("/api/search")
def search(q: str = Query(..., min_length=1),
           month: int = Query(7, ge=1, le=12),
           semantic: bool = True,
           max_price: float | None = None,
           destination: str | None = None,
           limit: int = Query(20, le=50)):
    try:
        rows = queries.hybrid_search(q, month, semantic=semantic,
                                     max_price=max_price, destination=destination, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"search failed: {e}")
    return {"query": q, "month": month, "semantic": semantic, "count": len(rows), "results": rows}


@app.get("/api/property/{property_id}")
def property_detail(property_id: int):
    row = queries.property_detail(property_id)
    if not row:
        raise HTTPException(status_code=404, detail="property not found")
    return row


@app.get("/api/pricing/{property_id}")
def pricing(property_id: int, month: int = Query(7, ge=1, le=12)):
    row = queries.pricing_for(property_id, month)
    if not row:
        raise HTTPException(status_code=404, detail="pricing not found")
    return row


@app.get("/api/pricing/{property_id}/curve")
def pricing_curve(property_id: int):
    return {"property_id": property_id, "curve": queries.price_curve(property_id)}


@app.get("/api/comps/{property_id}")
def comps(property_id: int, radius_mi: float = 20, month: int = Query(7, ge=1, le=12)):
    return {"property_id": property_id, "radius_mi": radius_mi,
            "comps": queries.comps_within(property_id, radius_mi, month)}


@app.get("/api/market")
def market(month: int = Query(7, ge=1, le=12)):
    return queries.market_summary(month)


class SaveReq(BaseModel):
    user_id: str = "demo-user"
    property_id: int


@app.post("/api/save")
def save(req: SaveReq):
    try:
        queries.save_property(req.user_id, req.property_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"save failed: {e}")
    return {"saved": True, "property_id": req.property_id}


class ApplyReq(BaseModel):
    property_id: int
    month: int
    applied_price: float
    action: str = "accept"          # "accept" or "override"
    host_id: str = "demo-host"


@app.post("/api/apply-price")
def apply_price(req: ApplyReq):
    try:
        result = queries.apply_price(req.property_id, req.month, req.applied_price,
                                     req.action, req.host_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"apply failed: {e}")
    return {"ok": True, **result}


@app.get("/api/decision/{property_id}")
def decision(property_id: int, month: int = Query(7, ge=1, le=12)):
    return queries.latest_decision(property_id, month) or {}


# Serve the static UI at "/". Mounted last so /api/* wins.
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
