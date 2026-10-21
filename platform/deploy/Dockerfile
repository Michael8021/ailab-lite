# Single image for all four Python platform services.
# Build (from the repo root, so the skills under images/ are in the context):
#         docker build -f platform/deploy/Dockerfile -t ailab-platform:lite .
# Run:    docker run --rm -p 8080:8080 ailab-platform:lite
#         (override command with e.g. `uvicorn ailab_platform.broker.main:app ...`)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY platform/pyproject.toml platform/constraints.txt platform/README.md ./
COPY platform/ailab_platform ./ailab_platform
COPY images/sbx-office/skills /opt/ailab/skills
COPY platform/models.toml /etc/ailab/models.toml
RUN pip install --upgrade pip \
 && pip install -c constraints.txt . \
 && pip install -c constraints.txt "opensandbox==1.1.1"

RUN useradd -u 10001 -m appuser \
 && mkdir -p /data \
 && chown -R 10001:10001 /data /app
USER 10001

ENV AILAB_SKILLS_DIR=/opt/ailab/skills \
    AILAB_MODELS_FILE=/etc/ailab/models.toml \
    AILAB_DATA_DIR=/data \
    AILAB_STORAGE_ROOT=/data/storage \
    AILAB_DATABASE_URL=sqlite:////data/platform.db

EXPOSE 8080 8081 8082 8083
CMD ["uvicorn", "ailab_platform.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
