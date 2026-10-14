# Secure mobile Talk deployment

This maintained local fork starts from upstream Bridge v1.0.11 at
`bc71a56c922cfc7d8797f62cb8f0fa6d2f37b39d`. It replaces the deployed bridge
entry point with a narrow service for private, per-user image conversations.
The original broad single-profile agent and local Nextcloud data/database media
access are not supported by this fork. Historical upstream tests are archived as
non-executable reference files under `tests/upstream_reference`; the current contract
is tested under `tests/secure`.

## Runtime and sleep

All services run in Docker containers. The host stores Compose configuration,
source, protected credentials and persistent volumes only. No Hermes dependency
is installed into the host Python environment.

- One lightweight Bridge owns a durable SQLite queue. At most one job runs globally.
- One isolated Runner per enabled user owns that user's credential and state volume.
- Hermes and its stdio MCP image server start only for a task and exit afterward.
- Save/discard decisions use deterministic host logic and do not start a model.
- Restarted in-flight work is marked interrupted, not automatically re-billed.
- Initial deployment enables only the explicitly selected TTauth owner/private room.
  A future user requires a new private room, dedicated credential, separate Runner
  container/state volume, and explicit binding. The LLM cannot choose its identity.

## Image lifecycle

1. Validate signed Talk event and stable UID/private room binding.
2. Check actual room participants using the authenticated user's Nextcloud API.
3. Start the pinned official Hermes CLI with only the task-bound image MCP tool.
4. Allow at most one billable image API request for that immutable task ID.
5. Verify generated PNG; upload to the same user's `AI Temporary` directory.
6. Share the native file attachment into the same private Talk room.
7. Proactively ask whether to save; `保存 <image-id>` moves that user's pending image
   to `AI Outputs/Images`. `不保存 <image-id>` leaves it temporary.

No public file share is created. No formal save is inferred from the model's text.
Temporary files are user-owned and no automatic deletion policy is enabled in this
first deployment. Save confirmation does not grant arbitrary agent shell access.
The current tool surface is image generation; broader agent tools are not enabled.

## Fixed sources and build

- Hermes official release: v0.22.0,
  `e06df9c2eae6901f4c88f464e0ae6e753c0428b8`.
- Python base: `python@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170`.
- Hermes core and MCP dependencies are built with its PM using the immutable
  release's lockfile; no moving `main` or unpinned `pip install hermes-agent`.
- Build context places that exact Hermes checkout at `runtime/hermes`; it is not
  committed to this bridge repository. Verify `git -C runtime/hermes rev-parse HEAD`.
- Build `Dockerfile.secure` targets `bridge` and `runner`. `Dockerfile` is identical.
- Populate server-only `.env` with `BRIDGE_IMAGE=sha256:...` and
  `RUNNER_IMAGE=sha256:...` from locally built image IDs. Compose never pulls tags.
- Start with `docker compose --env-file .env -f compose.yaml up -d` in the deployment
  directory. Preserve prior image IDs and state volumes for rollback.

The legacy AppAPI package manifest is upstream reference material. This fork uses
native Talk bot registration and Compose; App Store packaging/publishing is disabled.
No remote fork or public container registry has been created by this deployment.

## Security boundaries

No public host ports, Docker socket, Nextcloud global data mount, database credential,
sudo file reads, root container user, shell/file tools, shared family profile, exposed
lifecycle control, or unsigned command ingress. Config files are private on the host
and mounted read-only. Containers use read-only root filesystems, capability drops,
no-new-privileges, process/memory limits and separate writable state volumes.

API tokens retain their owner's existing Nextcloud account privileges; the worker
uses delegated file APIs. The Hermes child is not given the Nextcloud token and has
only the task-bound image MCP tool. The Bridge holds only bot/runner dispatch secrets;
it does not mount the user's model or Nextcloud credential file.

Before adding a user, repeat cross-user authorization checks against actual API
responses and container volume/credential boundaries. A filename or model prompt is
not an authorization boundary. Group/public conversations are rejected.

## Validation and operations

`python -m unittest discover -s tests/secure -v` runs offline contract tests.
Run these in the built bridge image as well, with no network and ephemeral `/tmp`.
CI tests before its manual image build; it does not automatically publish.

Health endpoints report service readiness and whether a Hermes process is active.
Use `docker compose ps`, `docker stats --no-stream`, and the private state databases
for diagnostics. Agent logs remain local and may contain user prompts; never publish
state directories, `.env`, credentials or diagnostic logs.

Actual mobile attachment display, notifications and lock-screen/reconnect behavior
require a real phone test. Server API checks do not establish those client results.
