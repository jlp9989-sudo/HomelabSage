# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .


FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="HomelabSage"
LABEL org.opencontainers.image.description="AI-powered homelab analyzer and update tracker"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/HomelabSage/HomelabSage"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import httpx,sys;sys.exit(0 if httpx.get('http://127.0.0.1:8000/healthz',timeout=3).status_code==200 else 1)"

ENTRYPOINT ["homelabsage"]
CMD ["serve", "--config", "/app/config.yaml"]
