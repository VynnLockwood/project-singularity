FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    EMBED_MODEL=intfloat/multilingual-e5-small \
    HF_HOME=/models

WORKDIR /app
COPY requirements.txt requirements-model.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-model.txt

# Bake the model into the image: the container has no internet at runtime.
RUN python -c "from sentence_transformers import SentenceTransformer as S; S('${EMBED_MODEL:-intfloat/multilingual-e5-small}', device='cpu')"
ENV HF_HUB_OFFLINE=1

COPY app.py .
RUN useradd --system --no-create-home app && chown -R app /app /models
USER app

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
