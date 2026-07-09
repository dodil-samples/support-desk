"""K3 client: object store (S3), SQL warehouse (tables), and vector search.

All three pillars over the K3 HTTP REST API, authed with the shared service-account
bearer + org headers. One class instance is scoped to one bucket.

Endpoints (verified against the dodil CLI's HTTP adapter):
  objects   PUT/GET  /{bucket}/{key}            list GET /{bucket}?list-type=2
  tables    POST     /{bucket}/tables                       (create)
            POST     /{bucket}/tables/_execute              (arbitrary SQL read)
            POST     /{bucket}/tables/{t}/insert|merge|delete-rows|_compact
  vector    POST     /{bucket}/vector                       (ensure engine, mode auto)
            POST     /{bucket}/vector/pipelines             (add embedding collection)
            POST     /{bucket}/vector/search                (semantic search)
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from xml.etree import ElementTree

from . import auth, http

BASE = os.getenv("K3_API_BASE", "https://k3.dev.dodil.io").rstrip("/")

# Column-type + freshness enums travel on the wire as their full protojson names.
T_STRING, T_LONG, T_INT, T_DOUBLE, T_BOOL, T_TS = (
    "COLUMN_TYPE_STRING",
    "COLUMN_TYPE_LONG",
    "COLUMN_TYPE_INT",
    "COLUMN_TYPE_DOUBLE",
    "COLUMN_TYPE_BOOLEAN",
    "COLUMN_TYPE_TIMESTAMP",
)


def col(name: str, type_: str, nullable: bool = True) -> dict:
    return {"name": name, "type": type_, "nullable": nullable}


class K3Error(RuntimeError):
    pass


class K3:
    def __init__(self, bucket: str):
        self.bucket = bucket
        # When set (by a budgeted block like ensure_vector), soft calls clamp their
        # per-call timeout to the remaining budget and skip once it's spent — so a
        # sequence of stuck provisioning calls can never blow the function limit.
        self._deadline: float | None = None

    # -- auth headers -------------------------------------------------------
    def _headers(self, content_type: str = "application/json") -> dict:
        h = {
            "Authorization": f"Bearer {auth.get_token()}",
            "x-organization-id": auth.org_id(),
            "x-organization-name": auth.org_name(),
        }
        if content_type:
            h["Content-Type"] = content_type
        return h

    def _post(self, path: str, body: dict, timeout: float = 60) -> object:
        status, data = http.request_json(
            "POST", f"{BASE}{path}", headers=self._headers(), json_body=body, timeout=timeout
        )
        if status >= 300:
            raise K3Error(f"POST {path} -> HTTP {status}: {str(data)[:240]}")
        return data

    # -- bucket -------------------------------------------------------------
    def ensure_bucket(self, description: str = "") -> None:
        # Idempotent: a 409/already-exists is fine.
        http.request_json(
            "POST",
            f"{BASE}/admin/buckets",
            headers=self._headers(),
            json_body={"name": self.bucket, "description": description},
            timeout=30,
        )

    # -- objects (S3) -------------------------------------------------------
    def put_object(self, key: str, body: bytes | str, content_type: str = "text/plain") -> None:
        if isinstance(body, str):
            body = body.encode()
        status, data = http.request(
            "PUT", f"{BASE}/{self.bucket}/{key}",
            headers=self._headers(content_type), data=body, timeout=60,
        )
        if status >= 300:
            raise K3Error(f"PUT object {key} -> HTTP {status}: {data[:200]!r}")

    def get_object(self, key: str) -> bytes:
        status, data = http.request(
            "GET", f"{BASE}/{self.bucket}/{key}", headers=self._headers(""), timeout=60
        )
        if status >= 300:
            raise K3Error(f"GET object {key} -> HTTP {status}")
        return data

    def list_objects(self, prefix: str = "") -> list[dict]:
        """List objects under a prefix — [{key, size, last_modified}].

        The endpoint speaks S3 ListObjectsV2, which answers in XML; parse it
        with stdlib ElementTree (namespace-agnostic via the localname match)."""
        q = f"?list-type=2&prefix={urllib.parse.quote(prefix)}" if prefix else "?list-type=2"
        status, data = http.request(
            "GET", f"{BASE}/{self.bucket}{q}", headers=self._headers(""), timeout=30)
        if status >= 300:
            raise K3Error(f"LIST objects {prefix!r} -> HTTP {status}")
        out = []
        try:
            root = ElementTree.fromstring(data)
            for el in root.iter():
                if el.tag.rsplit("}", 1)[-1] == "Contents":
                    row = {c.tag.rsplit("}", 1)[-1]: (c.text or "") for c in el}
                    out.append({"key": row.get("Key", ""),
                                "size": int(row.get("Size") or 0),
                                "last_modified": row.get("LastModified", "")})
        except ElementTree.ParseError:
            pass  # empty/odd body — treat as no objects
        return out

    def delete_object(self, key: str) -> None:
        status, _ = http.request(
            "DELETE", f"{BASE}/{self.bucket}/{key}", headers=self._headers(""), timeout=30)
        if status >= 300 and status != 404:
            raise K3Error(f"DELETE object {key} -> HTTP {status}")

    # -- warehouse (SQL tables) --------------------------------------------
    def create_table(self, name: str, columns: list[dict],
                     merge_keys: list[str] | None = None,
                     partition_columns: list[str] | None = None) -> object:
        # NB: the create endpoint keys the table on `name`; the row endpoints
        # (insert/merge/compact) key on `table_name`. Not a typo — two schemas.
        return self._post(f"/{self.bucket}/tables", {
            "bucket": self.bucket,
            "name": name,
            "columns": columns,
            "merge_keys": merge_keys or [],
            "partition_columns": partition_columns or [],
        })

    def execute(self, sql: str, freshness: str = "FRESHNESS_STRONG") -> list[dict]:
        """Run a read query; returns a list of row dicts."""
        data = self._post(f"/{self.bucket}/tables/_execute",
                          {"bucket": self.bucket, "sql": sql, "freshness": freshness})
        return _rows_to_dicts(data)

    def insert(self, table: str, rows: list[dict]) -> object:
        """Insert rows. On a merge-keyed table this UPSERTS (dedups by the key),
        so it's safe to re-run — the row-write endpoints expect `table_name` and
        each row as a JSON-encoded string."""
        return self._post(f"/{self.bucket}/tables/{table}/insert", {
            "bucket": self.bucket, "table_name": table,
            "rows": [json.dumps(r) for r in rows],
        })

    # Upsert-by-merge-key is just an insert on a merge-keyed table.
    upsert = insert

    def compact(self, table: str) -> object:
        # Flush the write-log so multi-table JOINs see just-written rows.
        return self._post(f"/{self.bucket}/tables/{table}/_compact",
                         {"bucket": self.bucket, "table_name": table})

    # -- vector -------------------------------------------------------------
    def ensure_vector(self, collection: str, template_id: str = "text_embedding_index",
                     include_patterns: list[str] | None = None) -> None:
        """Idempotently ensure engine + embedding collection + ingest rule.

        NB: the ConfigureEngine endpoint is NOT idempotent server-side — each
        auto-mode call allocates a brand-new VBase instance. So only configure the
        engine when the bucket has none, or a second call would create a duplicate
        vector DB for the same bucket.

        The whole sequence runs under a wall-clock budget: a stuck backend can slow
        each call, but the block as a whole stays bounded and just retries on a
        later invocation, so it never eats the request's function-timeout.
        """
        self._deadline = time.monotonic() + 30
        try:
            eng = self._get_soft(f"/{self.bucket}/vector")
            if not (eng.get("engineId") or eng.get("engine_id")):
                self._post_soft(f"/{self.bucket}/vector", {"bucket": self.bucket, "mode": "ENGINE_MODE_AUTO"})
            cols = self._get_soft(f"/{self.bucket}/vector/collections").get("collections", [])
            pipe = next((c for c in cols if c.get("embedPipelineName") == template_id), None) \
                or next((c for c in cols if collection in (c.get("name") or "")), None)
            if not pipe:
                pipe = self._post_soft(f"/{self.bucket}/vector/pipelines",
                                      {"bucket": self.bucket, "name": collection,
                                       "template_id": template_id})
            pipeline_id = (pipe or {}).get("embedPipelineId")
            srcs = self._get_soft(f"/{self.bucket}/sources").get("sources", [])
            source_id = srcs[0]["sourceId"] if srcs else None
            rules = self._get_soft(f"/{self.bucket}/rules").get("rules", [])
            if source_id and pipeline_id and not rules:
                self._post_soft(f"/{self.bucket}/rules", {
                    "bucket": self.bucket, "source_id": source_id, "name": f"{collection}-rule",
                    "include_patterns": include_patterns or ["**"],
                    "pipeline_id": pipeline_id, "enabled": True,
                })
        finally:
            self._deadline = None

    def has_vector_collection(self) -> bool:
        cols = self._get_soft(f"/{self.bucket}/vector/collections").get("collections", [])
        return len(cols) > 0

    def trigger_ingest(self) -> None:
        self._deadline = time.monotonic() + 15
        try:
            srcs = self._get_soft(f"/{self.bucket}/sources").get("sources", [])
            if not srcs:
                return
            sid = srcs[0]["sourceId"]
            self._post_soft(f"/{self.bucket}/sources/{sid}/discover",
                           {"bucket": self.bucket, "source_id": sid, "full_sync": True})
            self._post_soft(f"/{self.bucket}/sources/{sid}/ingest",
                           {"bucket": self.bucket, "source_id": sid})
        finally:
            self._deadline = None

    def vector_search(self, query: str, top_k: int = 5, min_score: float | None = None) -> list[dict]:
        # Vector search embeds the query (model inference, ~0.7s warm) and can hit a
        # Milvus cold segment-load (~5-10s) on an idle collection. Give it a dedicated
        # timeout and retry once: a cold-load / saturated-embedder first attempt warms
        # things so the retry returns fast. Best-effort — degrade to [] on failure.
        timeout = float(os.getenv("K3_VECTOR_TIMEOUT", "20"))
        body = {"bucket": self.bucket, "text": query, "top_k": top_k, "include_content": True}
        if min_score is not None:
            body["min_score"] = min_score
        data = None
        for attempt in (1, 2):
            try:
                status, data = http.request_json(
                    "POST", f"{BASE}/{self.bucket}/vector/search",
                    headers=self._headers(), json_body=body, timeout=timeout,
                )
                if status < 300 and isinstance(data, dict):
                    break
            except Exception:
                data = None  # timeout/abort — one more try
            if attempt == 2:
                return []
        if not isinstance(data, dict):
            return []
        out = []
        for m in data.get("results") or []:
            out.append({
                "text": (m.get("content") or m.get("text") or m.get("key") or "").strip(),
                "key": m.get("key"),
                "score": m.get("score"),
            })
        return out

    # -- soft helpers (never raise; provisioning is best-effort) ------------
    # Swallow BOTH HTTP errors and timeouts, and keep the per-call budget tight, so
    # the one-time vector provisioning can never block or fail the request (a slow
    # VBase call is skipped and retried on a later invocation). When a budget
    # deadline is set, clamp the per-call timeout to what's left and skip entirely
    # once it's spent, so a run of stuck calls stays bounded overall.
    def _budgeted_timeout(self, timeout: float) -> float | None:
        if self._deadline is None:
            return timeout
        remaining = self._deadline - time.monotonic()
        if remaining <= 0.5:
            return None  # budget spent — signal "skip this call"
        return min(timeout, remaining)

    def _post_soft(self, path: str, body: dict, timeout: float = 8) -> dict:
        t = self._budgeted_timeout(timeout)
        if t is None:
            return {}
        try:
            _, data = http.request_json("POST", f"{BASE}{path}", headers=self._headers(),
                                        json_body=body, timeout=t)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _get_soft(self, path: str, timeout: float = 8) -> dict:
        t = self._budgeted_timeout(timeout)
        if t is None:
            return {}
        try:
            _, data = http.request_json("GET", f"{BASE}{path}", headers=self._headers(""), timeout=t)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _rows_to_dicts(data: object) -> list[dict]:
    """Normalize an Execute response into a list of row dicts.

    K3 returns query rows as JSON-encoded strings (possibly under `rows` or
    `query.rows`), each decoding to an object (or an array aligned with `columns`).
    """
    if not isinstance(data, dict):
        return []
    q = data.get("query") if isinstance(data.get("query"), dict) else data
    rows = q.get("rows") or []
    columns = q.get("columns") or []
    out = []
    for r in rows:
        if isinstance(r, str):
            try:
                r = json.loads(r)
            except ValueError:
                continue
        if isinstance(r, dict):
            out.append(r)
        elif isinstance(r, list) and columns:
            out.append({columns[i]: r[i] for i in range(min(len(columns), len(r)))})
    return out
