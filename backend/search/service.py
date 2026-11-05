import re

from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import Case, ExpressionWrapper, F, FloatField, Q, Value, When
from django.db.models.functions import Lower
from pgvector.django import CosineDistance

from config.siconfig import get_config

from . import embedder
from .models import SchemaEmbedding

VECTOR_WEIGHT = 0.75   # ranking = meaning (vector) + name similarity, so names count but meaning leads
EXACT_BOOST = 0.5      # the query is exactly this table's/column's name: it goes first, whatever the model says


def search(query: str, top_k: int | None = None) -> dict:
    query = (query or "").strip()
    if not query:
        raise embedder.SearchError("query is required")
    cfg = get_config()["search"]
    k = max(1, min(top_k or cfg["topK"], 20))
    qvec = embedder.embed([query[:500]], "query")[0]
    name_query = query.lower()[:100]
    squashed = re.sub(r"[\s_\-]+", "", name_query)

    own_name = Case(When(kind="table", then=F("table_name")), default=F("column_name"))
    cos = Value(1.0) - CosineDistance("embedding", qvec)
    sim = TrigramSimilarity(own_name, name_query)
    exact = Case(When(Q(name_lower=squashed), then=Value(1.0)), default=Value(0.0), output_field=FloatField())
    score = ExpressionWrapper(
        Value(VECTOR_WEIGHT) * cos + Value(1 - VECTOR_WEIGHT) * sim + Value(EXACT_BOOST) * exact,
        output_field=FloatField(),
    )
    rows = (SchemaEmbedding.objects
            .annotate(name_lower=Lower(own_name), cos=cos, sim=sim)
            .annotate(score=score).order_by("-score")[:k])
    results = [{
        "kind": r.kind, "table": r.table_name, "column": r.column_name or None,
        # honest confidence: a strong meaning match or a strong name match, whichever is higher
        "confidence": round(max(0.0, min(1.0, max(r.cos, r.sim, 1.0 if r.name_lower == squashed else 0.0))), 3),
        "description": r.content,
    } for r in rows]
    confident = bool(results) and results[0]["confidence"] >= cfg["minConfidence"]
    return {
        "results": results,
        "confident": confident,
        "suggestion": None if confident else
        "Nothing matches well. Ask the assistant to propose a new column or table for this.",
    }
