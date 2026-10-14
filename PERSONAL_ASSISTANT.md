# Personal online assistant — maintained local fork

The current application is a general Hermes assistant reached through Nextcloud
Talk. The image-only v1 deployment is historical; its AI Temporary workflow must
not be used for new tasks. Initial access remains limited to the selected TTauth
account. Family onboarding has not been performed.

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

Optional Talk commands:

- `新项目 名称`: create a private project with the owner, delivery-only assistant
  and registered bot. Unknown rooms remain denied by the Bridge.
- `新任务 标题`: create a real named task thread. Enter that thread to continue.
- `项目列表`, `任务列表`, `帮助`: deterministic navigation without a model call.
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
bridge-image test for 46 contracts. Actual endpoint tests cover native project
creation, rename, emoji/PNG avatar, assistant-authored thread attachment, correct
owner download and negative permissions. A real task exercises MCP download,
terminal calculation, text artifact delivery and process exit. A natural main
assistant request reused a registered project, handed off into a distinct native
task, produced a budget artifact and reported back. Native MCP question → numbered
reaction → same-session resume passed; a separate task retained its budget and
did not acquire the other task's test memory. The user confirmed real mobile
image opening and a reaction choice with the correct confirmation reply.
Mobile thread navigation, push notifications and family deployment require
separate verification; don't infer them from backend tests.

Do not blindly restart v1: it rejects the newly added helper participant and does
not understand the new thread/consent workflow. Preserve new queue/connector
state and account credentials for a deliberate rollback. Stop the four services
to disable this assistant; existing Nextcloud Assistant/Context Agent are separate.
