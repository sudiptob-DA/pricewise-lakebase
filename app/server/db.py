"""
Lakebase connection + query helpers for PriceWise.

Uses the Databricks SDK to mint a short-lived (60-min) OAuth credential per new connection,
via a psycopg connection pool. Works both locally (DEFAULT profile in ~/.databrickscfg) and
inside a Databricks App (service principal env vars).
"""
import os
import functools
from databricks.sdk import WorkspaceClient
import psycopg
from psycopg_pool import ConnectionPool

PROJECT_ID = os.environ.get("LAKEBASE_PROJECT", "pricewise-db")
PGDATABASE = os.environ.get("LAKEBASE_DATABASE", "databricks_postgres")
PGPORT = os.environ.get("LAKEBASE_PORT", "5432")
EMBED_ENDPOINT = os.environ.get("EMBEDDING_ENDPOINT", "databricks-gte-large-en")

_w = WorkspaceClient()


@functools.lru_cache(maxsize=1)
def _resolve_endpoint():
    """Discover the project's production branch + primary endpoint (host + endpoint name)."""
    branch = next(iter(_w.postgres.list_branches(parent=f"projects/{PROJECT_ID}")))
    endpoint = next(iter(_w.postgres.list_endpoints(parent=branch.name)))
    host = endpoint.status.hosts.host if (endpoint.status and endpoint.status.hosts) else None
    if not host:
        raise RuntimeError("Lakebase endpoint host not ready (compute may be waking).")
    return host, endpoint.name


def _pguser():
    return _w.current_user.me().user_name


class _OAuthConnection(psycopg.Connection):
    """psycopg connection that fetches a fresh Lakebase OAuth token on each new connection."""

    @classmethod
    def connect(cls, conninfo="", **kwargs):
        _host, endpoint_name = _resolve_endpoint()
        kwargs["password"] = _w.postgres.generate_database_credential(endpoint=endpoint_name).token
        return super().connect(conninfo, **kwargs)


@functools.lru_cache(maxsize=1)
def _pool() -> ConnectionPool:
    host, _endpoint = _resolve_endpoint()
    conninfo = f"dbname={PGDATABASE} user={_pguser()} host={host} port={PGPORT} sslmode=require"
    return ConnectionPool(conninfo=conninfo, connection_class=_OAuthConnection,
                          min_size=1, max_size=8, open=True)


def query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read query, return list of dict rows."""
    with _pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def execute(sql: str, params: tuple = ()) -> None:
    """Run a write statement."""
    with _pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


def embed(text: str) -> str:
    """Embed a query string via Model Serving; return a pgvector literal '[...]'."""
    resp = _w.serving_endpoints.query(name=EMBED_ENDPOINT, input=[text])
    vec = resp.data[0].embedding
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
