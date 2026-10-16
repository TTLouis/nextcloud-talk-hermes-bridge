# Personal online assistant — maintained local fork

The current application is a general Hermes assistant reached through Nextcloud
Talk. The image-only v1 deployment is historical; its AI Temporary workflow must
not be used for new tasks. Access is provisioned explicitly for each account.
Every new user receives a private main conversation and a welcome/reaction card; drive consent remains
unset until that user makes the real choice.

## Projects and tasks

`主助理 · Hermes` is the primary assistant. Dedicated conversations use
`项目 · <subject>` (or an explicitly named `主题 · <subject>`). Native create
and rename calls enforce the role prefix, so a project cannot be titled as the
primary assistant. Users state their needs naturally here;
project/task commands are optional navigation shortcuts. For work deserving
separate follow-up, the assistant selects a relevant registered project or
creates a private conversation, starts a named task thread, and hands off a
structured summary, goals, decisions, next steps, file references and explicitly
selected scratch files. The globally serialized queue runs that task and reports
back to the primary assistant. The source chat and its session remain intact.
Full chat history and unrelated task memory are not copied into the child.

One private Talk conversation is one project. One real Talk thread is one task;
thread zero is the primary assistant or that project's coordination context.
The identity key is (stable Nextcloud UID, conversation token, real thread ID),
never a display name, prompt label or a global latest session. Each key owns a
Hermes HOME, SQLite session history, memory and scratchpad. Session IDs come from
Hermes' structured stream-json result and are resumed only for that key.

The worker receives the current native project and thread names. It can obtain
task names/IDs in the same project through MCP `project_tasks`, which strips the
other threads' first/last chat messages. No other task history is auto-injected.
An explicitly shared project directory is available, separate from task notes.
This is context separation for one user's projects; the security boundary between
different users is a separate worker container, volume, connector and credential.

Choices use native reaction cards: ✅/❌ for consent, save and changes; 1️⃣–5️⃣
for an assistant question. Clicking a card resumes its exact task. Mirrored cards
in the main conversation resolve to the same owned operation. Only the bound
owner can answer, the first valid choice wins, and removal/repeated/opposite
reactions cannot execute the operation again. A general question answer never
grants drive-reading or saving permission.

Ordinary answers, status updates and control confirmations are top-level
messages in the primary conversation, without automatically quoting the user's
last request. In dedicated tasks they use the task's thread membership without
quoting each individual message. Cards retain their exact operation/context
binding in private state; a quoted reply is not used as authorization evidence.

New task requests receive a signed bot 👀 reaction after durable enqueue.
A separate lightweight feedback sender can report a queue wait after 15 seconds,
the start of processing after 3 seconds, and continued processing every 180
seconds while the same job remains running. Destinations come from the stored
owner/project/thread, including the originating mirrored question card for a
resumed answer. Text contains no task prompt or other user's queue details.
Controls do not produce agent-running notices; onboarding waits are not
described as running. Reaction attempts are bounded, text is attempted once per
interval, and optional feedback failures do not retry the agent task.
Finalization is serialized with bounded feedback sends so running notices cannot
be emitted after the final result is released. Restarted in-flight jobs retain
the existing interrupted/no-automatic-retry behavior.

Queue/running text uses one helper-owned message per job. The existing
connector-specific bearer controls a dedicated feedback route; worker/model
grants cannot use it. Delivery state binds the message to its exact owner,
room and thread. Before editing, the helper re-fetches the exact native message
and verifies its own author and thread, without using moderator privileges.
The first final text or choice card replaces the status message; cards keep
their original authorization and reaction rules. A lost status receipt cannot
discard a final answer. Different-thread results or interrupted jobs trigger
bounded cleanup only after pending result notifications are delivered. Talk
may show its native deletion marker. Historical untracked bot notices are not
bulk-deleted. No API scopes, account permissions or model tools are added.

The main assistant keeps its continuous context and is instructed to act on
relevant, already-authorized next steps, organize independent work through the
existing handoff tools, and report useful next steps. The current request takes
priority; unrelated previous work is not silently restarted. No `/new` reset,
background monitoring, scheduled inference or broader API grants are added.

## Mobile replies and readable output paths

The agent is instructed to use short natural paragraphs and useful bullet points,
including material amounts, deadlines, risks and choices in the mobile text. For
a long explanation it can separate a complete mobile summary from the full
explanation with `<!-- assistant-details -->`. A deterministic fallback labels
an excerpt when no bounded summary is available. This is presentation only, not
a permission boundary. The full untrimmed model result is retained verbatim; the
former 12,000-character final-result truncation is removed.

The Worker writes a job-bound UTF-8 Markdown snapshot through descriptor-relative
IO. The Connector reads only that exact task path, verifies the digest and keeps
a protected snapshot bound to the owner/room/thread. A 📄 reaction delivers it
as an assistant-owned native attachment without a new agent run or consuming
a model override. Before that click it remains in Docker. Formal cloud saving
requires the separate existing ✅/❌ card. Main-conversation mirrored detail
cards resolve to the same original project/task operation and cannot cross users.

New outputs have sanitized Unicode names and frozen human-readable folder
mappings: `AI Outputs/个人助理/` or `AI Outputs/<project>/<task>/`. Internal context
and ownership keys remain opaque. Same-name folders/files receive numeric
suffixes. Existing artifacts retain their original displayed destination. Native
conditional uploads prevent overwrites; a 412 collision produces a new path and
a fresh confirmation rather than silently saving elsewhere. Old reservations
remain reserved. Project renames do not move saved files or pending destinations.

## Direct Talk images and coordination receipts

Signed native Create/Activity file events for PNG/JPEG/WebP/GIF become a single
deduplicated task with its real owner/room/thread and native message/file IDs.
Helper/bot attachments and other system/file types remain ignored. The Connector
re-fetches only that native message, checks the original user and resolves the
canonical thread (Talk file webhooks can omit it), verifies
its file ID and ETag through the same user's DAV namespace, and downloads with
If-Match. Webhook URLs/paths are not download authorities. This exact-attachment
input does not grant arbitrary drive reads or alter first-use consent.

Originals are capped at 10 MiB and stored only in the task scratchpad. The Worker
uses the existing pinned Pillow in its sealed Hermes runtime to reject invalid
or oversized rasters (20 million pixels, dimensions up to 12,000), transpose and
remove EXIF, composite transparency, take only the first animation frame, and
produce a JPEG up to 2048 pixels and 2 MiB. It passes that fixed job-local image
to Hermes' native `--image` input with the already-approved task model. Both
approved models were checked through the existing gateway; no auxiliary model,
new credential or remote image downloader is enabled. Model-request bodies are
bounded at 16 MiB for image history; MCP tool bodies retain their 4 MiB limit.

Chat, Responses and image-generation calls carry Connector-owned UUID session
headers derived from the user's exact conversation/thread. Project conversations
reference the main assistant as their parent; task threads reference their
project conversation. IDs stay stable across turns, model changes and renames,
and differ across users. These observability IDs do not grant access to context.

The source User-Agent includes bounded, readable user/conversation/task labels,
room/thread IDs, job ID and call sequence. Chinese titles are preserved. The
local audit records the same labels and IDs, route, actual model, time and HTTP
status, without prompts, images, responses or credentials. Operators who can
view gateway usage can see these conversation/task titles. Worker-supplied
session aliases and metadata cannot replace the authorized identity.

The installed CLIProxyAPI 8.0.10 usage queue was verified with its actual binary
against a fixed mock provider in a network-disabled container: session headers,
parent links and UTF-8 source labels are retained for chat and image generation.
Custom `hermes_*` metadata alone is not a usage-dashboard grouping mechanism.
The installed Usage Keeper 1.15.9 exposes these readable source labels in
Request Events → Columns → User Agent. Hover or focus a truncated cell to
see its full source label. Native Talk probes were also matched to actual Usage
Keeper records and checked in the UI. Session IDs remain UUIDs. Existing
historical audit rows are
preserved, and missing historical labels are not invented. The Connector audit
is attribution evidence, not an authorization boundary.

Before an agent turn, the Connector injects at most six bounded handoff result
receipts whose source exactly matches this owner/room/thread. It revalidates
access to each target private room and excludes removed/restricted rooms, raw
chat history and handoff source prompts. Receipts distinguish running, done,
awaiting_choice, failed and incomplete states. They are untrusted result data,
not new instructions or authorization.

A structured nonzero Hermes result with useful final text is returned as
incomplete rather than discarded. The original text is retained by the same
mobile-detail flow, task state and parent notification identify it as unfinished,
and the billable task is not retried automatically. Empty failures still fail
closed with the existing generic error notice.

## Text model choices

The initial per-user default is `gpt-6.1-sol`. The approved menu also offers
`gpt-5.6-terra`; this list is operator-controlled and bounded, never an arbitrary
model name or HTTP endpoint supplied by the agent.

Send `模型`, `模型设置`, `切换模型` or `/model` to open a deterministic two-stage
reaction card. First choose personal default, this conversation/thread's next
task, clear the pending single-use override, or cancel; then choose the model.
The personal default applies across that user's projects. A single-use override
is bound to the exact UID/room/thread and consumed by the next valid agent
request there, including its automatic handoff and question-choice continuations.
Consent, save, menu and other control operations do not consume it. Another
conversation cannot consume it. The next independent request uses the personal
default again. A new menu supersedes older menus in the same context.

Preferences and pending overrides persist in the user's Connector database.
Each task receives an immutable model receipt before its grant is issued;
changing the default does not alter a running task. Handoff inheritance must
match a registered handoff relationship; question continuations use the stored
origin job. Duplicate job dispatch reuses its receipt without consuming another
override. Worker CLI uses an explicit approved `--model` even when resuming the
same Hermes session, and the Connector proxy accepts only that task's frozen
model. Model choice never grants reading/saving permission or changes credentials.

Optional Talk commands:

- `新项目 名称`: create a private project with the owner, delivery-only assistant
  and registered bot. Unknown rooms remain denied by the Bridge.
- `新任务 标题`: create a real named task thread. Enter that thread to continue.
- `项目列表`, `任务列表`, `帮助`: deterministic navigation without a model call.
- `模型` or `/model`: personal-default and next-task model choice cards, without
  an inference request.
- `同意读取网盘`, `暂不授权`, `撤销网盘授权`: explicit user-owned read consent.
- `保存 <artifact-id>`, `不保存 <artifact-id>`: decide about a delivered result
  in its own task thread. Save asks show the proposed destination first.
- `确认操作 <proposal-id>`: confirm an assistant proposal to create/rename a
  project or change its PNG/emoji icon. Confirmation is bound to the same thread.

## Tools, scratchpad and consent

First use shows a deterministic welcome guide covering capabilities, automatic
project handoff, the meaning of a Talk task thread, Docker scratch storage,
assistant-owned delivery and reaction choices. The user then chooses ✅ to allow
task-directed cloud reads or ❌ to continue without them. The first request is
durably held without launching Hermes and resumes in its original user/project/
thread after either choice. More requests during setup wait behind the same
welcome card. Restart recovery releases only never-run setup requests when that
same user's persisted choice exists; interrupted billable jobs remain stopped.
Existing granted/declined choices are preserved on upgrade. `使用指南`, `帮助`
or `重新介绍` displays the guide again without a model call or permission reset.

Hermes runs general local file/terminal/todo/memory tools inside the worker.
Its MCP facade provides `nextcloud_list`, `nextcloud_read_text`,
`nextcloud_download`, `assistant_projects`, `handoff_project`, `project_tasks`,
`ask_user`, `generate_image`, `present_file` and `propose_project_change`.
Cloud files are read through the bound user's native
Nextcloud API behind MCP; native ACLs continue to apply. The model cannot select
another UID, another credential or a different destination server.

On first task use, the assistant asks whether the user permits task-directed
reading of files accessible to that account, including files shared to it. A
missing or declined consent makes every drive read fail closed. Revocation is
applied through an immediate control route even when a model task is running;
future reads are then denied. No automatic full-drive indexing/scanning is added.
Reading consent does not authorize saving, deleting or overwriting cloud files.

Workspaces persist under the deployment's per-user `workspace-*` volume:
`projects/<stable-project-key>/tasks/<context-key>/{hermes,scratch}`. Project shared
material is under `projects/<stable-project-key>/shared`. Intermediate files do
not go into the user's Nextcloud Files tree. Notes belong in scratch/TASK.md or
that task's memory. The exact known v1 deployment preview has been migrated to
an assistant-authored attachment with byte-for-byte verification; its old
`AI Temporary/hermes-9e026139352e.png` was removed with an ETag condition.
Unrelated files and the folder itself were not removed. This did not formally
save the preview into the user's outputs.

## Four container roles

- Bridge: signed Talk ingress, registered UID/project bindings, durable global
  one-task queue, deterministic commands and durable outbound notifications.
- Per-user Connector: that user's Nextcloud credential, read consent and artifact
  snapshots; issues short-lived task grants and proxies only fixed model routes.
- Per-user Worker: general Hermes and MCP, only its own workspace and unprivileged
  dispatch configuration; no Nextcloud token, provider API key, Docker socket,
  connector state or direct Nextcloud/model network.
- Delivery: a native local Database account `hermes-assistant`, no admin group,
  a random 512-bit password and a dedicated revocable application password.
  No Authentik account or SSO registration is created. The account holds only
  files explicitly selected for delivery, not the agent's entire scratchpad.

The worker's model key is an expiring task grant, not the upstream provider key.
Only the connector's fixed model can be called, with bounded model/tool request
counts. Connector filesystem reads/writes traverse descriptors with O_NOFOLLOW,
so worker-controlled symlinks cannot expose connector secrets. Worker jobs have
timeouts, log/file/process limits and a soft workspace budget; this is not a
filesystem-enforced disk quota. Detached descendants are killed when tasks end.

All runtimes/dependencies remain inside Docker. Hermes and its MCP subprocess
exist only during a task, while lightweight HTTP services remain running. Global
concurrency stays one. The fixed official Hermes v0.22.0 and Python base digests
are unchanged. Containers run as 1000:1000, read-only root, cap_drop ALL,
no-new-privileges, no published ports and no host Docker socket.

## Attachments and saving

Talk 23.0.11's BotController text endpoint has no file parameter. Native attachment
messages therefore come from the separate non-admin `Hermes 助手` delivery account.
`talkMetaData.threadId` keeps them in the intended task thread. The normal Talk
share may appear in the recipient's `Talk` shared-file area; this delivery copy
is not a user-root scratchpad or a formally saved user-owned output.

The Connector snapshots each explicitly presented output, delivers it, and the
Bridge proactively asks whether to save using a reaction card. Only the owner's
exact confirmation, including a card mirrored in the primary conversation,
uploads a user-owned copy to the displayed `AI Outputs/<project-token>/...`
destination, using If-None-Match to avoid overwriting existing files. The model
has no save/delete tool. A decline keeps the assistant's delivery and private
snapshot; no automatic expiry/deletion policy is enabled yet.

Generated PNGs may have dimensions different from the gateway's requested size.
Talk requires a square conversation icon. The assistant must make a square copy
inside its workspace before proposing an icon change; non-square proposals fail.

## Operation and evidence

Before enabling a family member, the operator provisions that member's own
stable UID binding, connector credential, isolated worker/network/volume and
private main room with the delivery account/bot, then repeats cross-user access
negative checks. Unknown users/rooms cannot self-enroll through a chat message.
Account credentials must not be requested in Talk. Only after authorized
provisioning does the member open their main conversation and complete the
welcome/reaction flow. A declined read choice still completes onboarding.

Deployment: `/opt/docker/hermes-talk`; use the server's protected `.env` and
`compose.yaml`. `compose.personal.yaml` is the four-role source template.
Two users produce six containers: shared Bridge/Delivery and one Connector/Worker
pair per user. `deploy/tenant_config.py` validates registered identities and
distinct dispatch/worker/delivery tokens, then expands the unchanged template
with a dedicated Worker network, workspace and Connector state for each user.
`deploy/deploy_personal.py` preserves all existing v2 tenant registrations,
credentials, delivery bindings, consent and model preferences on upgrade.

Explicit operator provisioning uses `deploy/add_tenant.py --uid STABLE_UID
--slug SAFE_SLUG --display-name EXACT_DISPLAY_NAME`. It verifies the native
identity, creates a revocable application token without changing the account's
password or SSO, creates/reconciles a private main room, invites the helper/bot,
stores protected per-user configs and regenerates Compose. No drive consent is
created. `deploy/onboard_tenant.py --slug SAFE_SLUG` sends the deterministic guide
and reaction card without a synthetic user request. Existing pending welcome
cards are reused when real requests arrive before the choice.

A two-user reference deployment was verified with a disposable private
conversation: native assistant-authored PNG download succeeded for its owner;
the other account received 404 for both the owner's file namespace and the same
random filename in its own namespace; anonymous access received 401. Both main-room cross-account requests returned
404. Cross-UID/room dispatch, Worker execution and Delivery room/question binding
failed with 401/403. The test conversation, delivery copy, marker and binding
were removed. The existing user's consent/settings/artifact records were
unchanged. No model task ran for the newly provisioned user and no read consent
was fabricated. A new user's actual mobile welcome choice and first real
model/image task must be tested separately by that user.
Build Dockerfile.secure targets bridge, connector, worker and delivery, then pin
immutable local image IDs. `deploy/deploy_personal.py` preserves v1 Compose/.env
backups, checks for active queue work, runs container contracts before switching,
and never prints generated credentials. Manual CI builds test before building
all four targets and do not publish images.

Native bot state now permits setup so newly created projects can register the
bot through the supported API. This does not grant a user access to an arbitrary
room: signed ingress still requires the stable UID and registered project, and
connectors/delivery recheck the exact owner/helper participant set.

Use `python -m unittest discover -s tests/secure -v` and the no-network disposable
bridge-image test for 76 contracts. Actual endpoint tests cover native project
creation, rename, emoji/PNG avatar, assistant-authored thread attachment, correct
owner download and negative permissions. A real task exercises MCP download,
terminal calculation, text artifact delivery and process exit. A natural main
assistant request reused a registered project, handed off into a distinct native
task, produced a budget artifact and reported back. Native MCP question → numbered
reaction → same-session resume passed; a separate task retained its budget and
did not acquire the other task's test memory. The user confirmed real mobile
image opening and a reaction choice with the correct confirmation reply.
Mobile thread navigation, push notifications and each new user's actual
onboarding require separate verification; don't infer them from backend tests.

Do not blindly restart v1: it rejects the newly added helper participant and does
not understand the new thread/consent workflow. Preserve new queue/connector
state and account credentials for a deliberate rollback. Stop the Compose services
to disable this assistant; existing Nextcloud Assistant/Context Agent are separate.
