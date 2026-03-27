FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Зависимости ставим отдельным слоем — кешируется при изменении только кода
COPY pyproject.toml .
RUN uv sync --no-dev

COPY . .

ENV PATH="/app/.venv/bin:$PATH"
