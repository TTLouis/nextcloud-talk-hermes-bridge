# Upstream and this fork

This fork derives from
[robertlmann02/nextcloud-talk-hermes-bridge](https://github.com/robertlmann02/nextcloud-talk-hermes-bridge).
The original MIT license and contributor attribution are retained.

## Comparison checkpoint

Reviewed on **2026-10-15** against upstream `main` at
[`b26267ff8ffb02b4e282b032815584163cff5021`](https://github.com/robertlmann02/nextcloud-talk-hermes-bridge/commit/b26267ff8ffb02b4e282b032815584163cff5021),
release **v1.0.12**. This fork's common ancestor with that revision is
[`bc71a56c922cfc7d8797f62cb8f0fa6d2f37b39d`](https://github.com/robertlmann02/nextcloud-talk-hermes-bridge/commit/bc71a56c922cfc7d8797f62cb8f0fa6d2f37b39d),
upstream **v1.0.11**. Seven commits on each side followed that common ancestor
before this documentation update.

The [archived upstream README](UPSTREAM_README.md) is a copy from the reviewed
v1.0.12 revision. It is reference material for upstream behavior, not installation
instructions for this fork.

## Changes added upstream since the common ancestor

| Upstream change | Relationship to this fork |
| --- | --- |
| AppAPI Talk bot registration during `/enabled`, with generated-secret persistence and unregister on disable | Upstream documents Talk/Spreed 24.0.5+ for this path. This fork uses manually provisioned native bots and a different Docker topology, verified on Talk 23.0.11. Auto-registration has not been ported. |
| Forward stable Talk actor identity into Hermes prompts and local memory | This fork already uses the signed event's stable UID for account/room authorization and per-user context. This is enforced by the Bridge and Connector, rather than relying on a prompt identity label. |
| Treat `/new` as an alias for resetting upstream short-term room context | This fork uses separate project/thread contexts and resumes the matching Hermes session. It does not expose upstream `/new` or `/reset`; `新任务 <title>` starts a separate task thread. |
| Updated timeout, heartbeat, approval and working-reaction defaults | These belong to upstream's environment-driven runtime. This fork uses its own task limits, globally serialized queue and operation-bound reaction cards. The upstream defaults were not applied. |
| Version metadata, AppAPI tests and release packaging | Retained as upstream changes for comparison. No fork App Store release or v1.0.12 compatibility claim is made. |

## What this fork adds

- A personal-assistant flow with explicit first-use onboarding and owner-controlled
  drive-read permission, including decline and immediate revocation.
- Separate per-user Connector/Worker services, credentials, state, workspaces and
  private Worker networks; signed webhook ingress and registered UID/room checks.
- A durable global queue with one task at a time, deduplication and outbound
  delivery retries without rerunning a billable model task.
- Per-project conversations and per-thread Hermes sessions, memory and scratchpads.
  Autonomous handoffs pass selected relevant context and report back to the main
  assistant.
- MCP tools for task-directed Nextcloud reads, project/task navigation, user
  questions, image generation, artifact presentation and proposed project changes.
- Native helper-authored Talk attachments and explicit reaction-card saving into
  the recipient's own output directory.
- Personal model defaults, context-bound next-task overrides and immutable model
  choices through handoffs and question continuations.
- Docker runtime hardening and 62 contracts covering these boundaries and flows.
- Multi-user provisioning and upgrades that preserve existing registrations,
  credentials, consent and model settings.

The entry point `nextcloud_talk_hermes_bridge.bridge` now delegates to
`assistant_bridge`. Upstream's tests that target the replaced implementation are
kept under `tests/upstream_reference/`; active contracts live in `tests/secure/`.
Upstream source helpers and release archives that remain in the tree should not
be assumed to be active features of this runtime.

## Update policy

Fetch and inspect upstream changes before porting them. Changes to bot lifecycle,
identity, credential handling or context must be adapted to this fork's actual
authorization and deployment paths, with relevant contract checks. A change to
the upstream README or package version alone does not establish compatibility.

This comparison did not merge upstream runtime changes or change a running
deployment. See [the user README](../README.md) for current capabilities and
[deployment notes](../SECURE_DEPLOYMENT.md) for the maintained runtime.

## Scoped adaptation after the comparison

The maintained runtime now adapts received/queued/running feedback using the
existing signed bot APIs. Its independent bounded sender preserves the one-job
queue, owner/project/thread routing and final-result ordering. It does not import
AppAPI lifecycle writes, upstream room-only approvals, identity fallbacks or the
60-minute timeout. Main-assistant instructions preserve continuous context and
limit initiative to relevant current requests and existing authorization.
Active Linux contracts now total 76, including feedback routing, replay, failure,
unsigned ingress, in-flight completion ordering and cross-user isolation.
