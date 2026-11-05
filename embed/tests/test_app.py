import os

os.environ["EMBED_AUTOLOAD"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import MAX_CHARS, MAX_TEXTS, create_app  # noqa: E402


class Fake:
    def __init__(self):
        self.seen = []

    def encode(self, texts):
        self.seen += texts
        return [[float(len(t))] * 3 for t in texts]


@pytest.fixture
def setup():
    fake = Fake()
    with TestClient(create_app(fake)) as client:
        yield fake, client


def test_adds_the_e5_prefix_for_each_kind(setup):
    fake, c = setup
    c.post("/embed", json={"texts": ["สวัสดี"], "kind": "query"})
    c.post("/embed", json={"texts": ["a", "b"], "kind": "passage"})
    assert fake.seen == ["query: สวัสดี", "passage: a", "passage: b"]


def test_returns_one_vector_per_text_in_order(setup):
    _, c = setup
    r = c.post("/embed", json={"texts": ["a", "bbb"], "kind": "passage"}).json()
    assert [v[0] for v in r["embeddings"]] == [len("passage: a"), len("passage: bbb")]


def test_empty_list_is_fine(setup):
    _, c = setup
    assert c.post("/embed", json={"texts": [], "kind": "query"}).json() == {"embeddings": []}


def test_rejects_bad_kind_and_too_many_texts(setup):
    _, c = setup
    assert c.post("/embed", json={"texts": ["a"], "kind": "document"}).status_code == 422
    assert c.post("/embed", json={"texts": ["a"] * (MAX_TEXTS + 1), "kind": "query"}).status_code == 422
    assert c.post("/embed", json={"kind": "query"}).status_code == 422


def test_long_text_is_cut_not_rejected(setup):
    fake, c = setup
    assert c.post("/embed", json={"texts": ["x" * (MAX_CHARS * 3)], "kind": "passage"}).status_code == 200
    assert len(fake.seen[0]) == len("passage: ") + MAX_CHARS


def test_health(setup):
    _, c = setup
    assert c.get("/healthz").json()["ok"] is True


def test_health_is_503_until_the_model_is_loaded():
    # no lifespan (not used as a context manager), no model yet
    c = TestClient(create_app(None))
    assert c.get("/healthz").status_code == 503
