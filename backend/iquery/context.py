"""What the model needs to know for a comment: which tables, which stored values, how the tables look."""
import difflib
import logging
import re

import psycopg
from psycopg import sql

from schema import introspect
from schema.conn import ro_connection
from search import service as search_service
from search.embedder import SearchError

log = logging.getLogger(__name__)

TEXT_TYPES = ("text", "character varying", "character")
VALUE_COLUMNS = 3          # columns per lookup, so it stays cheap
VALUES_PER_COLUMN = 3


def query_texts(comments: list[str]) -> list[str]:
    """Search texts, newest comment first, then widening to include the earlier ones."""
    if not comments:
        return []
    out = [comments[-1]]
    if len(comments) > 1:
        out.append("\n".join(comments[-2:]))
        if len(comments) > 2:
            out.append("\n".join(comments))
    seen: list[str] = []
    for t in out:
        t = t[:500]
        if t not in seen:
            seen.append(t)
    return seen


def memory_text(comments: list[str]) -> str:
    """The text whose embedding is stored with an example: the newest comment and the one before it."""
    return "\n".join(comments[-2:])


def _words(comments: list[str]) -> list[str]:
    """Distinct words of the comments, longest first (a company name beats a filler word)."""
    words = {w for w in re.split(r"[\s,;:()\[\]{}\"'`.!?]+", " ".join(comments)) if len(w) >= 3}
    return sorted(words, key=lambda w: (-len(w), w))[:12]


def _by_name_similarity(schema: str, comments: list[str]) -> list[str]:
    """Table names ranked by how close they are to the comment words; the fallback when search is offline."""
    words = [w.lower() for w in _words(comments)] or [" ".join(comments).lower()[:50]]
    with ro_connection() as conn:
        names = [r["t"] for r in conn.execute(
            "SELECT tablename::text AS t FROM pg_tables WHERE schemaname = %s ORDER BY tablename", (schema,))]
    def score(name: str) -> float:
        n = name.lower()
        return max(1.0 if (w in n or n in w) else difflib.SequenceMatcher(None, n, w).ratio() for w in words)
    return sorted(names, key=lambda n: (-score(n), n))[:20]


def pick_tables(schema: str, comments: list[str], limit: int) -> dict:
    """Tables to show the model: search hits (best text first), then their foreign-key neighbours, at most `limit`."""
    ranked: list[str] = []
    offline = False
    confident = False
    try:
        for i, text in enumerate(query_texts(comments)):
            res = search_service.search(text, 5, schema)
            if i == 0:
                confident = res.get("confident", False)
            for r in res["results"]:
                if r["table"] not in ranked:
                    ranked.append(r["table"])
    except SearchError:
        offline, confident = True, False
        ranked = _by_name_similarity(schema, comments)
    picked = ranked[:limit]
    neighbours: list[str] = []
    for r in introspect.graph(schema)["relations"]:
        a, b = r["from"]["table"], r["to"]["table"]
        if a in picked and b not in picked and b not in neighbours:
            neighbours.append(b)
        elif b in picked and a not in picked and a not in neighbours:
            neighbours.append(a)
    return {"tables": (picked + neighbours)[:limit], "searchOffline": offline, "confident": confident}


def find_values(schema: str, tables: list[str], comments: list[str], timeout_ms: int) -> list[dict]:
    """Stored text values that contain a word of the comments (a company name, say). Bounded by `timeout_ms`; never raises.

    Plain ILIKE on purpose: the read-only role cannot see the pg_trgm functions, and a bounded scan is enough here.
    """
    words = _words(comments)
    if not words:
        return []
    patterns = ["%" + re.sub(r"([\\%_])", r"\\\1", w) + "%" for w in words]
    found: list[dict] = []
    try:
        by_table = {t["name"]: t for t in introspect.graph(schema)["tables"]}
        with ro_connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            for t in tables:
                cols = [c["name"] for c in by_table.get(t, {}).get("columns", [])
                        if c["type"].split("(")[0] in TEXT_TYPES][:VALUE_COLUMNS]
                for c in cols:
                    q = sql.SQL("SELECT DISTINCT {c}::text AS v FROM {t} WHERE {c}::text ILIKE ANY (%s) "
                                "ORDER BY 1 LIMIT %s").format(c=sql.Identifier(c), t=sql.Identifier(schema, t))
                    for r in conn.execute(q, (patterns, VALUES_PER_COLUMN)):
                        found.append({"table": t, "column": c, "value": r["v"][:80]})
    except psycopg.Error:
        log.info("value lookup stopped early", exc_info=True)
        return []
    return found


def describe_for_prompt(schema: str, tables: list[str]) -> str:
    """One block per table: quoted name, columns with type and primary-key mark, then foreign keys."""
    graph = introspect.graph(schema)
    by_table = {t["name"]: t for t in graph["tables"]}
    blocks = []
    for t in tables:
        if t not in by_table:
            continue
        lines = [f'table "{t}"']
        for c in by_table[t]["columns"]:
            lines.append(f'  "{c["name"]}" {c["type"]}' + (" PRIMARY KEY" if c.get("primaryKey") else ""))
        for r in graph["relations"]:
            if r["from"]["table"] == t:
                lines.append(f'  FK "{r["from"]["column"]}" -> "{r["to"]["table"]}"."{r["to"]["column"]}"')
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
