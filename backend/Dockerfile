# Build context is the repository root (the Rust engine lives in ../engine).

# ---- stage 1: compile the Rust engine into a Python wheel (PyO3, abi3) ----
FROM python:3.13-slim AS engine
RUN apt-get update && apt-get install -y --no-install-recommends curl build-essential \
    && rm -rf /var/lib/apt/lists/*
ENV CARGO_HOME=/opt/cargo RUSTUP_HOME=/opt/rustup PATH=/opt/cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
RUN pip install --no-cache-dir maturin
WORKDIR /engine
COPY engine/ .
RUN maturin build --release --out /wheels

# ---- stage 2: the app ----
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# pg_dump is used for the backup taken before every schema change
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=engine /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

COPY backend/ .

RUN useradd --system --no-create-home app && mkdir /backups /scripts && chown -R app /app /backups /scripts
USER app

EXPOSE 8000
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--workers", "3"]
