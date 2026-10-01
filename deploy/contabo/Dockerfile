ARG PYTHON_BASE
FROM ${PYTHON_BASE}
ARG SOURCE_SHA
LABEL org.opencontainers.image.source="https://github.com/estebanSulcaInfante/envaperu-workflow" \
      org.opencontainers.image.revision="${SOURCE_SHA}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Explicit runtime allowlist. Never COPY the full checkout or a local .env.
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY run.py ./run.py
RUN mkdir -p /app/logs /app/instance && chown -R 10001:10001 /app
USER 10001:10001
EXPOSE 5000
CMD ["gunicorn", "run:app", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "60"]
