"""Matcher: which existing tables, columns, parent tables and rows an uploaded sheet corresponds to.

Local only: the embedding model (multilingual-e5) for columns, plain reads for references and rows, and the
duplicates engine for name matches. When the embedding service is down, column names are compared with difflib
and the result says `searchOffline`.
"""
import difflib
import hashlib
import json
import logging
import math
import re

from psycopg import sql

from approvals.models import Change
from config.siconfig import get_config
from duplicates import service as dup
from schema import introspect, roles
from schema.conn import ro_connection
from search import embedder
from search.embedder import SearchError
from search.models import SchemaEmbedding

from .models import ImportRecipe
from .profile import convert_value
from .profile import profile as profile_source

log = logging.getLogger(__name__)

BEST = 3                  # candidates kept per column, tables kept per sheet
EXAMPLES = 20             # row examples per sheet
PARENT_VALUES = 2_000     # values read per parent key column when looking for references
EXISTING_ROWS = 50_000    # existing rows read for name matching
REF_SHARE = 0.9           # share of distinct sample values that must equal a parent key
SAMPLES = 5               # samples per column when a source arrives without a profile


def _norm_header(h) -> str:
    return " ".join(str(h if h is not None else "").split()).lower()


def fingerprint(headers: list[str]) -> str:
    """sha256 of the sorted, trimmed, lower-cased header names (inner runs of spaces count as one)."""
    return hashlib.sha256(json.dumps(sorted(_norm_header(h) for h in headers)).encode()).hexdigest()


def _recipe(schema: str, sources: list) -> dict | None:
    headers = [h for s in sources for h in s["headers"]]
    if not headers:
        return None
    r = (ImportRecipe.objects.filter(schema=schema, fingerprint=fingerprint(headers),
                                     approved_change__status=Change.Status.APPLIED)
         .order_by("-updated_at").first())
    return {"id": r.pk, "mapping": r.mapping, "filters": r.filters} if r else None


def _entry_headers(entry) -> set:
    """The sheet headers a mapping entry uses: mapped, dropped and ref source columns."""
    if not isinstance(entry, dict):
        return set()
    hs = set((entry.get("columns") or {}).values()) if isinstance(entry.get("columns"), dict) else set()
    hs |= set(entry.get("drop") or []) if isinstance(entry.get("drop"), list) else set()
    for r in entry.get("refs") if isinstance(entry.get("refs"), list) else []:
        if isinstance(r, dict):
            hs.add(r.get("source_column"))
    return {_norm_header(h) for h in hs if isinstance(h, str)}


def rekey_recipe(recipe: dict, sources: list) -> dict:
    """A recipe is found by headers only, but its mapping and filters are keyed by the sheet names of the file it
    was approved for. Re-key them to this file's sheets: one sheet to one sheet, else by equal header sets.
    Entries that match no sheet stay as they are (the Critic then reports them)."""
    mapping = recipe.get("mapping") if isinstance(recipe.get("mapping"), dict) else {}
    tables = mapping.get("tables") if isinstance(mapping.get("tables"), dict) else {}
    names = [s["name"] for s in sources]
    rename = {}
    if len(tables) == 1 and len(names) == 1:
        rename = {next(iter(tables)): names[0]}
    else:
        free = {s["name"]: {_norm_header(h) for h in s["headers"]} for s in sources}
        for old, entry in tables.items():
            if old in free:
                rename[old] = old
                free.pop(old)
        for old, entry in tables.items():
            if old in rename:
                continue
            want = _entry_headers(entry)
            hit = next((n for n, hs in free.items() if want and hs == want), None)
            if hit:
                rename[old] = hit
                free.pop(hit)

    def rekeyed(d):
        return {rename.get(k, k): v for k, v in d.items()} if isinstance(d, dict) else d

    out = {**recipe, "mapping": {**mapping, "tables": rekeyed(tables)} if tables else recipe.get("mapping")}
    out["filters"] = rekeyed(recipe.get("filters"))
    if isinstance(recipe.get("skip"), dict):
        out["skip"] = rekeyed(recipe["skip"])
    return out


def column_text(col: dict) -> str:
    return f"{col['header']} ({col['type']}): {', '.join(col.get('samples') or [])}"


def _is_text(type_: str) -> bool:
    return dup._text({"type": type_})


# --- columns ---

def _unit(v) -> list[float]:
    v = [float(x) for x in v]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _by_embedding(schema: str, live: set, texts: list[str]) -> list[list] | None:
    """Best candidates per text by cosine with the schema's column documents; None when the index has none."""
    docs = [d for d in SchemaEmbedding.objects.filter(schema=schema, kind="column")
            .values_list("table_name", "column_name", "embedding") if (d[0], d[1]) in live]
    if not docs or not texts:
        return None
    vectors = embedder.embed(texts, "query")
    mat = [_unit(d[2]) for d in docs]
    out = []
    for v in vectors:
        q = _unit(v)
        sims = [sum(a * b for a, b in zip(row, q)) for row in mat]
        order = sorted(range(len(docs)), key=lambda i: (-sims[i], docs[i][0], docs[i][1]))[:BEST]
        out.append([[docs[i][0], docs[i][1], round(float(sims[i]), 4)] for i in order])
    return out


def _words(s: str) -> str:
    return re.sub(r"[\s_\-.]+", " ", s.lower()).strip()


def _by_name(live: set, headers: list[str]) -> list[list]:
    """The offline fallback: difflib similarity of the header with the column name (or table + column)."""
    out = []
    for h in headers:
        hn = _words(h)
        scored = []
        for t, c in live:
            s = max(difflib.SequenceMatcher(None, hn, _words(c)).ratio(),
                    difflib.SequenceMatcher(None, hn, _words(f"{t} {c}")).ratio())
            scored.append([t, c, round(s, 4)])
        out.append(sorted(scored, key=lambda x: (-x[2], x[0], x[1]))[:BEST])
    return out


def _table_scores(columns: dict) -> list[list]:
    totals: dict[str, float] = {}
    for cands in columns.values():
        if cands:
            totals[cands[0][0]] = totals.get(cands[0][0], 0.0) + cands[0][2]
    return [[t, round(s, 4)] for t, s in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:BEST]]


# --- references ---

class _Parents:
    """Single-column unique text keys of the schema's tables, with their values read on demand."""

    def __init__(self, schema: str, state):
        self.schema = schema
        self.keys = []
        for t in sorted(state):
            for key in sorted(state[t]["keys"], key=sorted):
                if len(key) == 1:
                    c = next(iter(key))
                    if _is_text(state[t]["columns"].get(c, {}).get("type", "")):
                        self.keys.append((t, c))
        self._values: dict[tuple, set] = {}

    def values(self, table: str, column: str) -> set:
        if (table, column) not in self._values:
            q = sql.SQL("SELECT DISTINCT {c}::text AS v FROM {t} WHERE {c} IS NOT NULL LIMIT {n}").format(
                c=sql.Identifier(column), t=sql.Identifier(self.schema, table), n=sql.Literal(PARENT_VALUES))
            with ro_connection() as conn:
                conn.execute("SET TRANSACTION READ ONLY")
                self._values[(table, column)] = {r["v"].strip().casefold() for r in conn.execute(q)}
        return self._values[(table, column)]


def _refs(col_profiles: list[dict], parents: _Parents, target: str | None) -> dict:
    out = {}
    for col in col_profiles:
        samples = {str(s).strip().casefold() for s in col.get("samples") or [] if str(s).strip()}
        if not samples:
            continue
        best = None
        for t, c in parents.keys:
            if t == target:
                continue      # the sheet's own table: a key column there is the row's key, not a reference
            share = len(samples & parents.values(t, c)) / len(samples)
            if share >= REF_SHARE and (best is None or share > best[0]):
                best = (share, t, c)
        if best:
            out[col["header"]] = {"table": best[1], "key": best[2]}
    return out


# --- rows ---

def _column_map(columns: dict, target: str) -> dict:
    """target column -> source header, one-to-one, best scores first."""
    pairs = sorted(((c[2], h, c[1]) for h, cands in columns.items() for c in cands if c[0] == target),
                   key=lambda p: -p[0])
    out, used = {}, set()
    for _, h, c in pairs:
        if c not in out and h not in used:
            out[c] = h
            used.add(h)
    return out


def _cells(src: dict, col_profiles: dict, header: str) -> list:
    i = src["headers"].index(header)
    col = col_profiles.get(header) or {}
    return [convert_value(row[i] if i < len(row) else None, col) for row in src["rows"]]


def row_key(state, target: str, cmap: dict) -> tuple[str | None, str | None]:
    """(key column, source header) used for the exact row match: the first single-column unique key (not the
    generated primary key) that `cmap` (target column -> source header) maps; (None, None) when none is."""
    pk = state[target]["pk"]
    keys = sorted(next(iter(k)) for k in state[target]["keys"] if len(k) == 1 and next(iter(k)) != pk)
    key = next((k for k in keys if k in cmap), None)
    return (key, cmap[key]) if key else (None, None)


def _rows(schema: str, src: dict, col_profiles: dict, columns: dict, target: str, state) -> dict:
    """The Matcher's row check, with its own guess of which header feeds which target column."""
    return row_check(schema, src, col_profiles, _column_map(columns, target), target, state)


def row_check(schema: str, src: dict, col_profiles: dict, cmap: dict, target: str, state) -> dict:
    """Which rows of `src` already exist (exact key) or may exist (name match) in `target`, when the sheet is
    loaded with `cmap` (target column -> source header). Records the key column and header it used."""
    key, header = row_key(state, target, cmap)
    out = {"table": target, "key": key, "header": header, "exists": [], "maybe": [], "examples": []}
    d = introspect.describe_table(target, schema)
    pk = state[target]["pk"]
    if d is None or pk is None or not src["rows"]:
        return out
    examples = []

    # exact match on a single-column unique key (not the generated primary key)
    exists_rows = set()
    if key:
        values = _cells(src, col_profiles, header)
        wanted = sorted({v for v in values if v is not None})
        q = sql.SQL("SELECT {pk} AS id, {k}::text AS k FROM {t} WHERE {k}::text = ANY(%s)").format(
            pk=sql.Identifier(pk), k=sql.Identifier(key), t=sql.Identifier(schema, target))
        with ro_connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            found = {r["k"]: r["id"] for r in conn.execute(q, (wanted,))} if wanted else {}
        for n, v in enumerate(values):
            if v is not None and v in found:
                out["exists"].append({"row": n + 1, "id": found[v]})
                exists_rows.add(n + 1)
                examples.append({"row": n + 1, "id": found[v], "kind": "exists", "incoming": v, "existing": v})

    # name match with the duplicates engine over existing plus incoming rows; cross pairs only
    columns_d = {c["name"]: c for c in d["columns"]}
    role_cols = roles.roles_of(d)
    fields = [f for f in dup.default_fields(columns_d, set(role_cols.values()) | dup.audit_columns(d)) if f in cmap]
    if fields:
        active = role_cols.get("is_active")
        q = sql.SQL("SELECT {pk} AS pk, {cols} FROM {t}{w} ORDER BY {pk} LIMIT {n}").format(
            pk=sql.Identifier(pk), cols=sql.SQL(", ").join(sql.Identifier(f) for f in fields),
            t=sql.Identifier(schema, target),
            w=sql.SQL(" WHERE {} IS DISTINCT FROM false").format(sql.Identifier(active)) if active else sql.SQL(""),
            n=sql.Literal(EXISTING_ROWS))
        with ro_connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            existing = {r["pk"]: [str(r[f]) if r[f] is not None else "" for f in fields] for r in conn.execute(q)}
        incoming_cols = [_cells(src, col_profiles, cmap[f]) for f in fields]
        incoming = {}
        for n in range(len(src["rows"])):
            vals = [col[n] or "" for col in incoming_cols]
            if (n + 1) not in exists_rows and any(vals):
                incoming[-(n + 1)] = vals
        best: dict[int, tuple] = {}
        if existing and incoming:
            threshold = float(get_config()["duplicates"]["threshold"])
            try:
                engine = dup._engine()
                found = engine.find_duplicates(
                    [(k, v) for k, v in existing.items()] + [(k, v) for k, v in incoming.items()], threshold)
            except dup.DuplicateError:
                log.warning("duplicates engine unavailable; no name matches", exc_info=True)
                found = []
            # The engine merges pairs into clusters with one (weakest-link) score, so a mixed cluster is
            # re-checked pair by pair: only an incoming row that directly matches an existing one is a maybe.
            for ids, _ in found:
                ex = sorted(i for i in ids if i > 0)
                for i in (i for i in ids if i < 0):
                    for e in ex:
                        hit = engine.find_duplicates([(e, existing[e]), (i, incoming[i])], threshold)
                        if hit and (-i not in best or hit[0][1] > best[-i][1]):
                            best[-i] = (e, float(hit[0][1]))
        for row in sorted(best):
            eid, score = best[row]
            text = " / ".join(v for v in existing[eid] if v)
            out["maybe"].append({"row": row, "id": eid, "score": round(score, 4), "existing": text})
            examples.append({"row": row, "id": eid, "kind": "maybe",
                             "incoming": " / ".join(v for v in incoming[-row] if v), "existing": text})
    out["examples"] = examples[:EXAMPLES]
    return out


# --- the whole match ---

def match(schema: str, sources: list, profiles: list[dict], row_match: bool) -> dict:
    result = {"recipe": _recipe(schema, sources), "searchOffline": False, "sources": {}}
    if not sources:
        return result
    graph = introspect.graph(schema)
    hidden = roles.hidden_columns()
    live = {(t["name"], c["name"]) for t in graph["tables"] for c in t["columns"] if c["name"] not in hidden}
    by_name = {p.get("name"): p for p in profiles or []}
    cols_per_source = []
    for src in sources:
        prof = by_name.get(src["name"]) or profile_source(src, SAMPLES)
        known = {c["header"]: c for c in prof.get("columns") or []}
        cols = [known.get(h) or {"header": h, "type": "text", "samples": []} for h in src["headers"]]
        cols_per_source.append(cols)

    texts = [column_text(c) for cols in cols_per_source for c in cols]
    headers = [c["header"] for cols in cols_per_source for c in cols]
    try:
        cands = _by_embedding(schema, live, texts)
    except SearchError:
        result["searchOffline"] = True
        cands = None
    if cands is None:
        cands = _by_name(live, headers)

    state = introspect.snapshot(schema)
    parents = _Parents(schema, state)
    k = 0
    for src, cols in zip(sources, cols_per_source):
        columns = {}
        for c in cols:
            columns[c["header"]] = cands[k]
            k += 1
        tables = _table_scores(columns)
        target = tables[0][0] if tables else None
        entry = {"tables": tables, "columns": columns, "refs": _refs(cols, parents, target)}
        if row_match and target in state:
            entry["rows"] = _rows(schema, src, {c["header"]: c for c in cols}, columns, target, state)
        result["sources"][src["name"]] = entry
    return result
