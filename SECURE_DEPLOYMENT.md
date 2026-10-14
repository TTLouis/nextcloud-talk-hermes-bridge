# Maintained personal assistant deployment

Use [PERSONAL_ASSISTANT.md](PERSONAL_ASSISTANT.md) for the current four-container
general assistant, autonomous project handoff, private Docker scratchpads,
owner-consented MCP reads, assistant-authored attachments and reaction choices.

Runtime: `/opt/docker/hermes-talk`. The active entry point is
`nextcloud_talk_hermes_bridge.assistant_bridge`; `bridge` imports it too.
Use `Dockerfile.secure` targets `bridge`, `connector`, `worker`, `delivery` and
`compose.personal.yaml`. All images are pinned in the protected server `.env`.

The previous image-only two-container implementation remains as historical
source and [deployment notes](docs/SECURE_DEPLOYMENT_V1.md). Its user-owned
temporary-folder workflow is superseded. Do not deploy `compose.secure.yaml` or
the historical `runner` target as the current application.

Check status with:

```sh
docker compose --env-file /opt/docker/hermes-talk/.env -f /opt/docker/hermes-talk/compose.yaml ps
```

Stop those four services to disable the assistant while preserving its state.
Do not use `down -v`. Before an upgrade, verify the global queue is idle; the
deployment helper checks both before building and immediately before switching.
Family onboarding is a separate deployment with a per-user connector, worker,
volume and credential; it has not been enabled.
