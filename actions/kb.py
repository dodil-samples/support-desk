"""Knowledge base: semantic search, article storage, grounded reply drafting."""

from __future__ import annotations

from lib import models

from .common import latest_customer_body, one, slug, sql_str


def kb_hits(k3, text: str, top_k: int = 3) -> list[dict]:
    try:
        return k3.vector_search(text, top_k=top_k)
    except Exception:
        return []


def search_kb(k3, p: dict) -> dict:
    return {"query": p.get("query", ""),
            "results": k3.vector_search(p.get("query", ""), top_k=int(p.get("top_k", 5)))}


def add_kb(k3, p: dict) -> dict:
    title = p.get("title", "Untitled")
    key = f"kb/{slug(title, 'article')}.md"
    k3.put_object(key, f"# {title}\n\n{p.get('body','')}", content_type="text/markdown")
    k3.trigger_ingest()  # index now rather than waiting for the periodic sync
    return {"kb_key": key, "note": "K3 is embedding it for suggested answers."}


def list_kb(k3, _p: dict) -> dict:
    """Every article = one object under kb/ — including files dropped straight
    into the bucket (`dodil k3 object create <bucket> kb/<name>.md --file …`);
    the ingest rule embeds anything matching kb/**, not just add_kb's output."""
    rows = k3.list_objects("kb/")
    return {"count": len(rows), "articles": rows}


def get_kb(k3, p: dict) -> dict:
    key = str(p.get("kb_key") or "")
    if not key.startswith("kb/"):
        return {"error": "kb_key must start with kb/"}
    try:
        return {"kb_key": key, "body": k3.get_object(key).decode("utf-8", "replace")}
    except Exception:
        return {"error": f"no article at {key!r}"}


def remove_kb(k3, p: dict) -> dict:
    """ADMIN-ONLY: delete an article. The vector index drops it on the next
    ingest sync (triggered here), so stale hits fade within a minute or so."""
    key = str(p.get("kb_key") or "")
    if not key.startswith("kb/"):
        return {"error": "kb_key must start with kb/"}
    k3.delete_object(key)
    k3.trigger_ingest()
    return {"removed": key}


def draft_reply(k3, subject: str, body: str) -> tuple[str, list[dict]]:
    """KB vector search → LLM-drafted grounded reply. Shared by the agent-facing
    suggest_reply action and the AI-agent worker (actions/routing.py)."""
    hits = kb_hits(k3, f"{subject}\n{body}")
    kb_context = "\n\n".join(f"[KB] {h['text'][:600]}" for h in hits) or "(no KB articles matched)"
    draft = models.chat([
        {"role": "system", "content":
            "You are a helpful support agent. Draft a concise, friendly reply that "
            "resolves the customer's issue. Ground it in the provided KB context; if "
            "the KB doesn't cover it, say what info you need. Do not invent policy."},
        {"role": "user", "content":
            f"Customer subject: {subject}\nCustomer message: {body}\n\n"
            f"Knowledge base context:\n{kb_context}"},
    ])
    return draft, hits


def suggest_reply(k3, p: dict) -> dict:
    tid = p["ticket_id"]
    t = one(k3, f"SELECT subject FROM tickets WHERE ticket_id={sql_str(tid)}")
    draft, hits = draft_reply(k3, t.get("subject", ""), latest_customer_body(k3, tid))
    return {"ticket_id": tid, "draft_reply": draft, "kb_used": hits}
