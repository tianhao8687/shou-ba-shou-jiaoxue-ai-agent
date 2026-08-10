FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS app-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 10001 harbor \
    && useradd --uid 10001 --gid harbor --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin harbor

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY --chown=10001:10001 backend/app /app/backend/app
COPY --chown=10001:10001 data /app/data

WORKDIR /app/backend

FROM app-base AS test

USER root
COPY backend/requirements-dev.txt /app/backend/requirements-dev.txt
RUN pip install --no-cache-dir -r /app/backend/requirements-dev.txt
COPY --chown=10001:10001 backend/tests /app/backend/tests
COPY --chown=10001:10001 backend/pyproject.toml /app/backend/pyproject.toml
COPY --chown=10001:10001 ops_sandbox /app/ops_sandbox
COPY --chown=10001:10001 local_model_service /app/local_model_service
COPY --chown=10001:10001 kubernetes_connector /app/kubernetes_connector
COPY --chown=10001:10001 scripts/check-coverage.py /app/scripts/check-coverage.py
ENV COVERAGE_FILE=/tmp/.coverage \
    PYTEST_ADDOPTS="-p no:cacheprovider"
USER harbor

FROM app-base AS runtime

USER harbor
EXPOSE 8000 9101

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
