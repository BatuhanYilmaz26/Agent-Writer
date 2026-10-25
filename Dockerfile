FROM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --gid 10001 fieldnote \
    && useradd --uid 10001 --gid 10001 --no-create-home \
       --shell /usr/sbin/nologin fieldnote

COPY requirements.lock ./
RUN python -m pip install --require-hashes --only-binary :all: -r requirements.lock

COPY --chown=10001:10001 src ./src
COPY --chown=10001:10001 migrations ./migrations

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "fieldnote", "serve"]
