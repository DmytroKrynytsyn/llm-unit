FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /build

COPY broker/pyproject.toml .
COPY broker/main.py ./broker/

RUN uv build --wheel --out-dir /dist


FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /dist/*.whl .

RUN pip install --no-cache-dir *.whl && rm *.whl

EXPOSE 8000

CMD ["uvicorn", "broker.main:app", "--host", "0.0.0.0", "--port", "8000"]
