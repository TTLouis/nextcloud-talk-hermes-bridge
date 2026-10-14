# Maintained personal assistant deployment

Use [PERSONAL_ASSISTANT.md](PERSONAL_ASSISTANT.md) for the current four-role
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

Stop the Compose services to disable the assistant while preserving its state.
Do not use `down -v`. Before an upgrade, verify the global queue is idle; the
deployment helper checks both before building and immediately before switching.
TTauth and MMFelix are explicitly provisioned. Each has its own Connector,
Worker, private Worker network, workspace, credential, consent and model state.
The Bridge/Delivery are shared, producing six containers with one global task
queue. `deploy/tenant_config.py` generates Compose from validated protected
registrations; upgrades retain every tenant instead of rebuilding a singleton.
MMFelix's welcome card awaits its owner's real allow/decline choice.

To provision an explicitly authorized new user, run server-local
`deploy/add_tenant.py --uid STABLE_UID --slug SAFE_SLUG --display-name EXACT_NAME`,
then repeat native ACL/dispatch negative checks and run
`deploy/onboard_tenant.py --slug SAFE_SLUG`. Never manufacture read consent or
collect account passwords in Talk. Unknown accounts cannot self-enroll.
