FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --no-dev --frozen --no-editable
ENV CTXSWITCH_DATA=/data PATH="/app/.venv/bin:$PATH"
VOLUME /data
EXPOSE 7433
CMD ["ctx-hub", "--bind", "0.0.0.0", "--port", "7433", "--db", "/data/hub.db"]
