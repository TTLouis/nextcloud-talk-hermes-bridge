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
Each explicitly provisioned user has its own Connector, Worker, private
Worker network, workspace, credential, consent and model state.
Bridge and Delivery are shared; two users produce six containers with one
global task queue. `deploy/tenant_config.py` generates Compose from validated protected
registrations; upgrades retain every tenant instead of rebuilding a singleton.
A new user's welcome card awaits its owner's real allow/decline choice.

To provision an explicitly authorized new user, run server-local
`deploy/add_tenant.py --uid STABLE_UID --slug SAFE_SLUG --display-name EXACT_NAME`,
then repeat native ACL/dispatch negative checks and run
`deploy/onboard_tenant.py --slug SAFE_SLUG`. Never manufacture read consent or
collect account passwords in Talk. Unknown accounts cannot self-enroll.


## Optional public research

[Playwright CLI browsing](browser/README.md) runs in a separate Docker project
with a public-only egress proxy and no published port. The two additional
containers receive no cloud/provider credential or user workspace mount.
Only registered Connectors join its private call network; Workers retain
their existing per-user network, mounts and privileges.

`deploy/public_browser.py` builds the pinned sidecar and generates unique control
tokens for already-registered users. The normal tenant renderer preserves these
network attachments during upgrades. Newly provisioned users do not inherit
another user's browser token; enable browsing explicitly after provisioning.

Assistant-drafted outbound queries require a bound owner choice. Exact URLs and
search terms supplied in the current native user message can be used directly;
model-authored handoffs are not implicit user authorization. Page contents and
downloads cannot grant drive reads/saves or account controls. Authentication
material is blocked from public requests. Existing image-request claims still
prevent duplicate billing; jobs and image-generation requests are not replayed.
A confirmed empty chat completion can reserve one extra non-streaming attempt
using the same selected model, context and tool permissions. This requires the
original active, unexpired task grant and counts against its model budget.
Persisted reservations prevent another recovery after restart. Other provider
errors, refusals and reasoning/tool responses do not trigger this recovery.
