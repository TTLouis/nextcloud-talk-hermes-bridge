# Hermes Personal Assistant for Nextcloud Talk

A self-hosted personal assistant you can use from the Nextcloud Talk mobile or
desktop app. Ask a question, work through a task, generate an image, or use your
Nextcloud files after granting read access.

This is a maintained fork of
[Nextcloud Talk Hermes Bridge](https://github.com/robertlmann02/nextcloud-talk-hermes-bridge),
powered by [Hermes Agent](https://github.com/NousResearch/hermes-agent).
It adds private workspaces, per-user permissions, project handoffs and native
Talk attachments. The current deployment uses Docker.

## What you can do

- **Get work done from a conversation.** Discuss ideas, plan a task, calculate,
  and create documents or other files using the assistant's private workspace.
- **Work with your Nextcloud files.** After you opt in, ask the assistant to find,
  read or download files your account can already access, including shared files.
- **Show the assistant a picture.** Send a PNG, JPEG, WebP or GIF in Talk; the
  assistant can inspect it with your selected model. GIFs use the first frame.
- **Generate images and receive files in Talk.** Open the returned attachment,
  then choose whether to save a copy to your own files.
- **Keep projects organized.** The main assistant can create or reuse a private
  project conversation, hand off relevant context, and report the result back.
- **Choose your model.** Set your personal default or override just the next task
  in the current conversation or thread.

For example:

> Read the notes in my trip folder and make a packing checklist.
>
> Generate a simple illustrated cover for this project.
>
> Help me plan the renovation. Keep it in its own project and report back here.

Available models and image generation depend on your administrator's configured
model service.

## Start using the assistant

Your administrator first connects your Nextcloud account to a private assistant
conversation. Open **`主助理 · Hermes`** in Talk and read the welcome card.

Choose **✅** to allow task-directed reads of your Nextcloud files, or **❌** to
continue without drive access. Either choice completes setup. You can still
chat, generate images and work with the private workspace after declining.
The assistant does not automatically scan your whole drive.

Then send a request normally. If you sent a request before completing setup,
it waits for your choice and continues afterward. Ordinary replies in the main
conversation appear as standalone messages. The main assistant keeps its
continuous context, follows relevant agreed next steps, and can organize
independent work without requiring a context-reset command.

When a decision is needed, tap the reaction shown below the question: **✅ / ❌**
for a yes/no choice, or **1️⃣–5️⃣** for multiple options. Each card is bound to its
own task and can only be answered by its owner.

A 👀 reaction acknowledges a newly queued task. If it waits more than 15 seconds,
the assistant sends a queue notice. Tasks still running after 3 seconds receive
one processing message, updated every 3 minutes while needed. The assistant
edits that same message into the first final answer or decision card, keeping
the conversation compact. If the answer belongs in a different thread, the
temporary message is deleted after delivery. Native Talk may retain a deletion
marker in that uncommon case. Status updates do not call a model or reveal
another user's tasks. They use the existing non-admin delivery account;
the agent receives no additional permissions.

A blank model reply can trigger one extra recovery attempt with your selected
model. This continues the current request; it does not restart a job or repeat
an image-generation request. If the assistant still cannot finish, it reports
the task as incomplete.

The current welcome messages and control commands are in Chinese. You can write
ordinary requests in the language supported by your chosen model.

## Public web research

Ask the assistant to search public pages, read sources or download a public PDF,
text file or image. Results include source links; downloads first stay in the
task's private Docker scratchpad. Delivering an attachment and saving it to your
drive remain separate steps.

Assistant-drafted searches show the exact outgoing query as a reaction choice.
The assistant can follow links it has found, but cannot silently construct a
new URL containing private text. Browsing uses isolated Playwright CLI sessions
and a public-only network proxy. See [web research and Docker setup](browser/README.md).

## Reading replies on a phone

Replies use short paragraphs and useful bullet points, with key results, amounts,
deadlines, risks and decisions in the chat. Longer explanations offer a **📄**
reaction to open the complete original as a Markdown attachment. Opening it does
not make another model call. Saving it to your own files still requires a
separate **✅** confirmation. If the assistant produces an unstructured long
reply, the chat labels its preview as an excerpt rather than claiming it is a
complete summary. The original text is retained in the private assistant state.

## Projects and tasks

Start in the main assistant; you do not need to create a project manually.

| Conversation | Purpose |
| --- | --- |
| `主助理 · Hermes` | Your main assistant for everyday requests and coordinating work. |
| `项目 · <name>` or `主题 · <name>` | A dedicated private conversation for a project or subject. |
| A named Talk thread inside a project | One task with its own context, notes and working files. |

A **thread** groups messages about one task inside a conversation. Open that
thread to continue the task; other task histories are not automatically added to
its context.

When the assistant hands work off, it passes the relevant background, decisions,
file references and next steps. Your original conversation stays in place;
unrelated history is not copied into the project. The project task runs through
the same queue and sends an update back to the main conversation. On your next
turn there, the assistant also receives bounded status/result receipts for its
linked handoffs, so it can discuss their progress without importing unrelated
project chats. A task that stops partway reports its available progress with an
explicit incomplete status; it is not automatically retried.

## Images, attachments and saving

The assistant sends finished files as native Talk attachments under the separate
**`Hermes 助手`** delivery account. Open the attachment first, then use its save
card:

- **✅ Save:** create your own copy at the destination shown on the card, under
  `AI Outputs/<project name>/<task name>/` (or `AI Outputs/个人助理/`
  for the main assistant).
- **❌ Do not save:** leave the delivered attachment available without creating
  a formal copy in your output folder.

Talk may show a delivered attachment in its shared-file area. Drafts, scratch
files and intermediate results stay in the assistant's Docker workspace.
Choosing not to save does not delete the delivered attachment or private
snapshot; automatic expiry is not currently implemented.

New outputs keep descriptive Unicode filenames, including Chinese. Duplicate
names receive a number such as `预算 (2).txt`. The save card shows the exact
destination; an existing cloud file is never overwritten. If a collision is
found when saving, a new destination needs a fresh confirmation. Existing files
and previously displayed destinations are preserved. Folder names are fixed
when first used, so renaming a conversation does not move saved files.

Send an image directly in the assistant conversation or its task thread. The
assistant verifies the original sender, conversation, thread and file identity
using your own account before reading it. Sending that image permits processing
that attachment; it does not grant general drive access. Existing first-use setup
still applies, and declining drive reads does not disable direct Talk images.
Images are limited to 10 MiB and normalized locally before model input: EXIF is
removed, the longest edge is at most 2048 pixels, and GIFs use only the first
frame. HEIC, SVG, video and arbitrary image URLs are not accepted by this input.
Small or unclear text may need a closer image.

## Model selection

Send **`模型`** or **`/model`**, then tap the cards to choose:

- **My default:** applies to your future tasks across your projects.
- **Next task only:** applies to the next task in this conversation or thread,
  including its handoff and follow-up choices. Later independent tasks return
  to your default.
- **Clear the next-task override** or **cancel**.

The reference configuration defaults to `gpt-6.1-sol` and also offers
`gpt-5.6-terra`. Your administrator controls the approved model list. Your choices
do not affect other users, and changing a default does not switch a running task.

## Useful commands

These are optional shortcuts; ordinary requests do not need a command prefix.

| Send in Talk | Action |
| --- | --- |
| `使用指南`, `帮助` or `重新介绍` | Show the usage guide again. |
| `模型`, `模型设置`, `切换模型` or `/model` | Open model settings. |
| `项目列表` | List your registered projects. |
| `任务列表` | List tasks in the current conversation. |
| `新项目 <name>` | Create a dedicated project conversation. |
| `新任务 <title>` | Create a named task thread. |
| `同意读取网盘` | Allow task-directed reads of files accessible to your account. |
| `撤销网盘授权` or `暂不授权` | Turn off drive reads. |

Save and other confirmation cards can normally be answered with reactions.

## Your data and permissions

Each provisioned user has separate credentials, a Worker container, workspace,
task history, consent and model preferences. File access uses that user's native
Nextcloud permissions through MCP tools. Permission to read files does not grant
permission to save, overwrite or delete them.

The delivery account has no administrator privileges. Long-lived Nextcloud and
model-service credentials stay outside the Worker; tasks receive temporary,
scoped access through their user's Connector.

One agent task runs at a time across the deployment. Hermes and its MCP
subprocesses start when a task runs and exit afterward; lightweight services
stay available. The Docker host must remain awake to receive new Talk messages.

## Self-hosting and current scope

The reference deployment has been verified with Nextcloud **33.0.3**, Talk
**23.0.11**, and Hermes **v0.22.0**, using an external model service. It uses a
shared Bridge and Delivery service plus one Connector and Worker per user.

Installation is currently an administrator-managed Docker deployment. The
deployment helpers reflect the reference topology and require adapting hostnames,
container names, networking and model configuration; they are not a portable
one-command installer.

- [Deployment and operations](SECURE_DEPLOYMENT.md)
- [Architecture, permissions and behavior](PERSONAL_ASSISTANT.md)
- [Upstream comparison and attribution](docs/UPSTREAM.md)

Gmail and other external account tools are not connected. The upstream voice,
uploaded-media, cron and AppAPI installation features are not exposed by this
fork's current assistant runtime. The retained upstream files and release
archives do not describe an installation path for this version.

## Development

Run the existing contracts with Python 3.11 or newer:

```sh
python -m unittest discover -s tests/secure -v
```

Run Linux-specific filesystem contracts in Linux or Docker. Runtime dependencies
for Hermes are built into the Worker image rather than installed on the host.

## License

[MIT](LICENSE). Original upstream attribution is retained. This fork is
independently maintained and is not an official Nextcloud or Hermes release.
