FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY config ./config
COPY data ./data
ENV PATH="/app/.venv/bin:$PATH" CASEFILE_ROOT=/app
EXPOSE 8000
CMD ["sh", "-c", "casefile db init && casefile serve --host 0.0.0.0 --port 8000"]
