ARG PYTHON_BASE=python@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
FROM ${PYTHON_BASE} AS bridge
WORKDIR /app
COPY nextcloud_talk_hermes_bridge /app/nextcloud_talk_hermes_bridge
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
ENTRYPOINT ["python","-m","nextcloud_talk_hermes_bridge.assistant_bridge"]

FROM ${PYTHON_BASE} AS runner
WORKDIR /opt/hermes
COPY runtime/hermes /opt/hermes
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HERMES_HOME=/tmp/hermes-build HERMES_RUNTIME_DIR=/opt/hermes/tools
RUN python -m pm.build_env --source /opt/hermes --python /usr/local/bin/python \
    --out /opt/hermes/.venv --no-install-project --sealed --record-selection --extra mcp
WORKDIR /app
COPY nextcloud_talk_hermes_bridge /app/nextcloud_talk_hermes_bridge
ENV PYTHONPATH=/app:/opt/hermes HERMES_HOME=/state/hermes
USER 1000:1000
ENTRYPOINT ["python","-m","nextcloud_talk_hermes_bridge.secure_runner"]

FROM bridge AS connector
ENTRYPOINT ["python","-m","nextcloud_talk_hermes_bridge.assistant_connector"]

FROM runner AS worker
ENTRYPOINT ["python","-m","nextcloud_talk_hermes_bridge.assistant_worker"]

FROM bridge AS delivery
ENTRYPOINT ["python","-m","nextcloud_talk_hermes_bridge.assistant_delivery"]
