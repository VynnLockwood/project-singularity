"""Embedding service: multilingual-e5-small on CPU, internal network only.

POST /embed {"texts": [...], "kind": "query" | "passage"} -> {"embeddings": [[...384 floats...], ...]}
E5 models need a 'query: ' or 'passage: ' prefix; the service adds it so callers cannot forget.
"""
import os
from contextlib import asynccontextmanager
from typing import Literal, Protocol

from fastapi import FastAPI, Response
from pydantic import BaseModel, Field

MODEL_NAME = os.environ.get("EMBED_MODEL", "intfloat/multilingual-e5-small")
MAX_TEXTS = 64
MAX_CHARS = 2000  # ~512 tokens is the model's limit; longer text is cut, not rejected


class Model(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class EmbedRequest(BaseModel):
    texts: list[str] = Field(max_length=MAX_TEXTS)
    kind: Literal["query", "passage"]


def load_model() -> Model:
    from sentence_transformers import SentenceTransformer  # heavy import, only when serving

    st = SentenceTransformer(MODEL_NAME, device="cpu")

    class Wrapped:
        def encode(self, texts):
            return st.encode(texts, normalize_embeddings=True, batch_size=16).tolist()

    return Wrapped()


def create_app(model: Model | None = None) -> FastAPI:
    holder = {"model": model}

    @asynccontextmanager
    async def lifespan(_):
        if holder["model"] is None:
            holder["model"] = load_model()  # load before serving so the first request is not slow
        yield

    app = FastAPI(title="singularity-embed", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def get_model() -> Model:
        if holder["model"] is None:
            holder["model"] = load_model()
        return holder["model"]

    @app.get("/healthz")
    def healthz(response: Response):
        ready = holder["model"] is not None
        response.status_code = 200 if ready else 503
        return {"ok": ready, "model": MODEL_NAME}

    @app.post("/embed")
    def embed(req: EmbedRequest):
        prefixed = [f"{req.kind}: {t[:MAX_CHARS]}" for t in req.texts]
        return {"embeddings": get_model().encode(prefixed) if prefixed else []}

    return app


app = create_app() if os.environ.get("EMBED_AUTOLOAD", "1") == "1" else None
