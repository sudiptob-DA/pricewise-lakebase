"""Genie Conversations API client for PriceWise.

A defensive wrapper over the Databricks Genie REST API. Credentials resolve via the
Databricks SDK automatically — the DEFAULT profile locally, or the injected service
principal when running as a Databricks App (no embedding admin setting required).

Pattern adapted from the author's CosmosGenie app (sudiptob-DA/CosmosGenie),
including the data_typed_array handling that otherwise silently yields zero rows.
"""
from __future__ import annotations
import json
import time
from typing import Any, Generator, Optional
from databricks.sdk import WorkspaceClient

TERMINAL_OK = {"COMPLETED"}
TERMINAL_FAIL = {"FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"}


class GenieError(RuntimeError):
    pass


class GenieClient:
    def __init__(self, space_id: str, w: Optional[WorkspaceClient] = None,
                 poll_interval: float = 1.5, timeout_seconds: float = 120.0, max_rows: int = 100):
        if not space_id:
            raise ValueError("space_id is required")
        self.space_id = space_id
        self.poll_interval = poll_interval
        self.timeout_seconds = timeout_seconds
        self.max_rows = max_rows
        self._w = w or WorkspaceClient()

    def _do(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        try:
            resp = self._w.api_client.do(method, path, body=body)
        except Exception as exc:
            raise GenieError(f"Databricks API call failed: {exc}") from exc
        return resp if isinstance(resp, dict) else {}

    def ask(self, question: str, conversation_id: Optional[str] = None) -> dict:
        """Ask Genie a question, wait for completion, return a JSON-serializable dict:
        {text, follow_up, sql, columns, rows, row_count, conversation_id, error, elapsed}."""
        question = (question or "").strip()
        if not question:
            raise ValueError("question must not be empty")
        started = time.monotonic()

        if conversation_id:
            path = f"/api/2.0/genie/spaces/{self.space_id}/conversations/{conversation_id}/messages"
            payload = self._do("POST", path, {"content": question})
            message = payload if "id" in payload else payload.get("message", {})
            cid = conversation_id
        else:
            path = f"/api/2.0/genie/spaces/{self.space_id}/start-conversation"
            payload = self._do("POST", path, {"content": question})
            message = payload.get("message", {})
            cid = (payload.get("conversation") or {}).get("id")

        mid = message.get("id") or message.get("message_id")
        if not (cid and mid):
            raise GenieError(f"No conversation/message id returned. Keys: {sorted(payload)}")

        final = self._poll(cid, mid)
        turn = self._build(cid, mid, question, final)
        turn["elapsed"] = round(time.monotonic() - started, 1)
        return turn

    # Human-friendly labels for Genie API statuses
    STATUS_LABELS = {
        "SUBMITTED": "Submitting question…",
        "FILTERING_CONTEXT": "Reading table schemas…",
        "ASKING_AI": "Writing SQL query…",
        "EXECUTING_QUERY": "Running query…",
        "COMPLETED": "Done",
        "FAILED": "Failed",
        "CANCELLED": "Cancelled",
    }

    def _poll(self, cid: str, mid: str) -> dict:
        path = f"/api/2.0/genie/spaces/{self.space_id}/conversations/{cid}/messages/{mid}"
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            payload = self._do("GET", path)
            status = payload.get("status", "UNKNOWN")
            if status in TERMINAL_OK or status in TERMINAL_FAIL:
                return payload
            time.sleep(self.poll_interval)
        raise GenieError(f"Genie did not finish within {self.timeout_seconds:.0f}s")

    def ask_streaming(self, question: str, conversation_id: Optional[str] = None) -> Generator[str, None, None]:
        """Like ask(), but yields SSE events with intermediate reasoning steps."""
        question = (question or "").strip()
        if not question:
            raise ValueError("question must not be empty")
        started = time.monotonic()

        if conversation_id:
            path = f"/api/2.0/genie/spaces/{self.space_id}/conversations/{conversation_id}/messages"
            payload = self._do("POST", path, {"content": question})
            message = payload if "id" in payload else payload.get("message", {})
            cid = conversation_id
        else:
            path = f"/api/2.0/genie/spaces/{self.space_id}/start-conversation"
            payload = self._do("POST", path, {"content": question})
            message = payload.get("message", {})
            cid = (payload.get("conversation") or {}).get("id")

        mid = message.get("id") or message.get("message_id")
        if not (cid and mid):
            raise GenieError(f"No conversation/message id returned. Keys: {sorted(payload)}")

        # Poll with intermediate status events
        poll_path = f"/api/2.0/genie/spaces/{self.space_id}/conversations/{cid}/messages/{mid}"
        deadline = time.monotonic() + self.timeout_seconds
        last_status = None
        while time.monotonic() < deadline:
            poll_payload = self._do("GET", poll_path)
            status = poll_payload.get("status", "UNKNOWN")
            if status != last_status:
                last_status = status
                label = self.STATUS_LABELS.get(status, f"Processing ({status})…")
                elapsed = round(time.monotonic() - started, 1)
                yield f"data: {json.dumps({'type': 'step', 'status': status, 'label': label, 'elapsed': elapsed})}\n\n"
            if status in TERMINAL_OK or status in TERMINAL_FAIL:
                turn = self._build(cid, mid, question, poll_payload)
                turn["elapsed"] = round(time.monotonic() - started, 1)
                yield f"data: {json.dumps({'type': 'result', **turn})}\n\n"
                return
            time.sleep(self.poll_interval)
        yield f"data: {json.dumps({'type': 'error', 'error': f'Genie did not finish within {self.timeout_seconds:.0f}s'})}\n\n"

    def _build(self, cid: str, mid: str, question: str, payload: dict) -> dict:
        status = payload.get("status", "UNKNOWN")
        turn = {"conversation_id": cid, "message_id": mid, "question": question,
                "status": status, "text": None, "follow_up": None, "sql": None,
                "columns": [], "rows": [], "row_count": 0,
                "queries": [], "error": None}
        if status in TERMINAL_FAIL:
            err = payload.get("error") or {}
            turn["error"] = err.get("error") or err.get("message") or status
            return turn

        # Collect ALL query attachments (Genie often runs multiple queries)
        query_attachments = []
        for att in payload.get("attachments") or []:
            text = att.get("text")
            if isinstance(text, dict) and text.get("content"):
                if text.get("purpose") == "FOLLOW_UP_QUESTION":
                    turn["follow_up"] = text["content"]
                else:
                    turn["text"] = text["content"]
            query = att.get("query")
            if isinstance(query, dict):
                sql = query.get("query") or ""
                desc = query.get("description") or query.get("title") or ""
                aid = att.get("attachment_id")
                turn["sql"] = sql or turn["sql"]  # backward compat: last SQL
                if aid:
                    query_attachments.append({"attachment_id": aid, "sql": sql, "description": desc})

        # Fetch results for ALL query attachments
        for qa in query_attachments:
            entry = {"description": qa["description"], "sql": qa["sql"],
                     "columns": [], "rows": [], "row_count": 0}
            try:
                self._fetch_result_into(cid, mid, qa["attachment_id"], entry)
            except GenieError:
                pass
            turn["queries"].append(entry)

        # Backward compat: populate top-level columns/rows from last query with data
        for q in reversed(turn["queries"]):
            if q["rows"]:
                turn["columns"], turn["rows"], turn["row_count"] = q["columns"], q["rows"], q["row_count"]
                break

        if not turn["text"] and not turn["follow_up"] and not turn["columns"]:
            turn["error"] = turn["error"] or "Genie returned an empty response."
        return turn

    def _fetch_result_into(self, cid: str, mid: str, attachment_id: str, target: dict) -> None:
        """Fetch query result and populate target dict with columns/rows/row_count."""
        path = (f"/api/2.0/genie/spaces/{self.space_id}/conversations/{cid}"
                f"/messages/{mid}/attachments/{attachment_id}/query-result")
        payload = self._do("GET", path)
        stmt = payload.get("statement_response") or {}
        manifest = stmt.get("manifest") or {}
        schema = manifest.get("schema") or {}
        cols_meta = schema.get("columns") or []
        target["columns"] = [c.get("name", f"col_{i}") for i, c in enumerate(cols_meta)]
        target["column_types"] = [c.get("type_name", "STRING") for c in cols_meta]
        target["rows"] = self._extract_rows(stmt.get("result") or {}, self.max_rows)
        target["row_count"] = manifest.get("total_row_count") or len(target["rows"])

    # Kept for backward compatibility with the non-streaming ask() callers
    def _fetch_result(self, cid: str, mid: str, attachment_id: str, turn: dict) -> None:
        self._fetch_result_into(cid, mid, attachment_id, turn)

    @staticmethod
    def _extract_rows(result: dict, max_rows: int) -> list[list[Any]]:
        # The API returns one of two shapes; handle BOTH or silently get zero rows.
        if result.get("data_array"):
            return [list(r) for r in result["data_array"][:max_rows]]
        typed = result.get("data_typed_array") or []
        rows = []
        for entry in typed[:max_rows]:
            values = entry.get("values") or []
            rows.append([v.get("str") if isinstance(v, dict) else v for v in values])
        return rows
