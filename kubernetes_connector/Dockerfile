FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

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
COPY --chown=10001:10001 kubernetes_connector /app/backend/kubernetes_connector

WORKDIR /app/backend
USER 10001:10001
EXPOSE 8094
CMD ["uvicorn", "kubernetes_connector.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8094"]
