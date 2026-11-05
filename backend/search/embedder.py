"""Client for the embed container (multilingual-e5-small). E5 needs 'query: ' / 'passage: ' prefixes; the service adds them."""
import requests
from django.conf import settings

BATCH = 32


class SearchError(Exception):
    pass


def embed(texts: list[str], kind: str) -> list[list[float]]:
    """kind is 'query' or 'passage'. Returns one 384-d unit vector per text."""
    if not settings.EMBED_URL:
        raise SearchError("search is not configured: EMBED_URL is not set")
    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        chunk = texts[i:i + BATCH]
        try:
            resp = requests.post(f"{settings.EMBED_URL}/embed", json={"texts": chunk, "kind": kind}, timeout=60)
        except requests.RequestException as exc:
            raise SearchError("the embedding service is unreachable") from exc
        if resp.status_code != 200:
            raise SearchError(f"the embedding service returned HTTP {resp.status_code}")
        try:
            vectors = resp.json()["embeddings"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SearchError("the embedding service sent an unreadable reply") from exc
        if len(vectors) != len(chunk):
            raise SearchError("the embedding service returned the wrong number of vectors")
        out += vectors
    return out
