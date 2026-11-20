# In The Know hosted connector. Stateless: no volume, nothing written to disk.
# Run ONE instance (see docs/launch-checklist.md): each owner's engine and write lock live in that instance's RAM.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY intheknow ./intheknow
RUN uv sync --frozen --no-dev

RUN useradd --uid 10001 --no-create-home itk
USER itk

ENV ITK_HOST=0.0.0.0 PORT=8080 PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health',timeout=2)"
CMD ["python", "-m", "intheknow.hosted"]
