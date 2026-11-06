"""Build one document per table/column of `master`, embed what changed, and keep the index in step."""
import hashlib
import logging
import threading

from django.conf import settings
from django.db import transaction
from psycopg import sql

from config.siconfig import get_config
from schema import introspect, roles
from schema.conn import ro_connection

from . import embedder
from .models import DIMENSIONS, SchemaEmbedding

log = logging.getLogger(__name__)

SAMPLE_TYPES = ("text", "character varying", "integer", "bigint", "smallint", "numeric", "date", "boolean")
MAX_SAMPLE_LEN = 40
_lock = threading.Lock()


def _samples(table: str, column: str, limit: int) -> list[str]:
    q = sql.SQL("SELECT DISTINCT {c}::text AS v FROM master.{t} WHERE {c} IS NOT NULL LIMIT {n}").format(
        c=sql.Identifier(column), t=sql.Identifier(table), n=sql.Literal(limit))
    try:
        with ro_connection() as conn:
            return [str(r["v"])[:MAX_SAMPLE_LEN] for r in conn.execute(q)]
    except Exception:
        log.warning("could not sample %s.%s", table, column, exc_info=True)
        return []


def build_docs() -> list[dict]:
    g = introspect.graph()
    refs = {(r["from"]["table"], r["from"]["column"]): r["to"] for r in g["relations"]}
    n_samples = get_config()["search"]["sampleValues"]
    docs = []
    conv = roles.conventions()
    for t in g["tables"]:
        d = introspect.describe_table(t["name"])
        skip = set(roles.roles_of(d, conv).values()) | roles.hidden_columns(conv)
        comments = {c["name"]: c["comment"] for c in d["columns"]}
        text = f"table {t['name']}."
        if d["comment"]:
            text += f" Description: {d['comment']}."
        text += " Columns: " + ", ".join(c["name"] for c in t["columns"] if c["name"] not in skip) + "."
        docs.append({"kind": "table", "table": t["name"], "column": "", "content": text})
        for c in t["columns"]:
            if c["name"] in skip:
                continue
            text = f"table {t['name']}, column {c['name']} ({c['type']})."
            if comments.get(c["name"]):
                text += f" Description: {comments[c['name']]}."
            if (t["name"], c["name"]) in refs:
                to = refs[(t["name"], c["name"])]
                text += f" References {to['table']}.{to['column']}."
            if n_samples and c["type"].split("(")[0] in SAMPLE_TYPES:
                vals = _samples(t["name"], c["name"], n_samples)
                if vals:
                    text += " Examples: " + ", ".join(vals) + "."
            docs.append({"kind": "column", "table": t["name"], "column": c["name"], "content": text})
    return docs


def reindex(*, rebuild: bool = False) -> dict:
    """Embed new or changed documents, drop ones whose table/column is gone. Safe to run concurrently (serialised)."""
    with _lock:
        docs = build_docs()
        for d in docs:
            d["hash"] = hashlib.sha256(d["content"].encode()).hexdigest()
        existing = {(e.kind, e.table_name, e.column_name): e for e in SchemaEmbedding.objects.all()}
        todo = [d for d in docs if rebuild or (d["kind"], d["table"], d["column"]) not in existing
                or existing[(d["kind"], d["table"], d["column"])].content_hash != d["hash"]]
        vectors = embedder.embed([d["content"] for d in todo], "passage") if todo else []
        for v in vectors:
            if len(v) != DIMENSIONS:
                raise embedder.SearchError(f"the embedding model returned {len(v)} dimensions, expected {DIMENSIONS}")
        live = {(d["kind"], d["table"], d["column"]) for d in docs}
        with transaction.atomic():
            for d, v in zip(todo, vectors):
                SchemaEmbedding.objects.update_or_create(
                    kind=d["kind"], table_name=d["table"], column_name=d["column"],
                    defaults={"content": d["content"], "content_hash": d["hash"], "embedding": v})
            gone = [e.pk for k, e in existing.items() if k not in live]
            SchemaEmbedding.objects.filter(pk__in=gone).delete()
        return {"embedded": len(todo), "unchanged": len(docs) - len(todo), "removed": len(gone)}


def schedule_reindex() -> None:
    """Fire and forget after a schema change. Off when no embedding service is configured."""
    if not settings.EMBED_URL:
        return

    def run():
        try:
            reindex()
        except Exception:
            log.warning("background reindex failed", exc_info=True)

    threading.Thread(target=run, daemon=True, name="search-reindex").start()
