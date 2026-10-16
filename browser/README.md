# Public web research through Playwright CLI

The assistant can search public pages, read sources and download PDF, UTF-8 text,
CSV, JSON, PNG, JPEG and WebP into its current Docker task scratchpad. It uses
[Microsoft's Playwright CLI](https://github.com/microsoft/playwright-cli), pinned
to0.1.22 with an npm integrity lockfile and matching Chromium headless shell.

The first version supports public browsing. It does not reuse your browser
profile, website logins, Nextcloud credentials or model provider credentials.
There is no arbitrary JavaScript/CLI/form/upload tool exposed to the agent.

## Usage

Ask for a public search, provide a document URL, or ask the assistant to follow
a source it has found. It returns source links and can deliver downloaded files
through the existing `present_file` attachment flow. Formal cloud saving still
requires your separate confirmation.

The default is one owner choice when a project first needs the public web.
The card shows its conversation name and first outgoing query/URL. Allow the
whole conversation (including all task threads), allow only this request,
block the conversation, or decline this request. Project approval permits
subsequent assistant-drafted public queries/URLs without repeated prompts.

Send `/设置` or `/settings` to change web permission: ask-first, automatic
approval, blocked, or strict per-request review. The main conversation can be
set to automatic approval independently. Its first-use card also offers an
explicit combined main-plus-future-project default; this does not change other
existing projects. Each new project snapshots its owner's current default.
Permissions and defaults persist in the owner's isolated Connector database.

In strict mode, assistant-drafted queries and unobserved URLs require an exact
request choice; exact `搜索：...` / `search: ...`, native user URLs and observed
same-task public sources retain the prior direct-use behavior. Model-authored
handoffs are never native user URLs. Existing pending approvals migrate as
one-off approvals, without silently granting a whole project. Changing a project
setting invalidates its old pending and unexecuted grants across task threads.

All modes retain credential blocking (including common URL/base64 encodings),
public-only egress and operation/download limits. Auto approval permits the
assistant to draft outgoing queries/URLs; it removes their individual human
review. The assistant remains instructed not to send private files or full
conversations, and public pages remain untrusted data. Approval is never cloud
read/write or save consent, and a model tool or page cannot modify settings.

## Boundaries

- Web pages, search snippets and downloads remain untrusted data. They cannot
  authorize cloud reads/saves, change an account, or consume another task's choice.
- Eight public operations per root task; page text is limited to16000 characters.
  Documents are limited to20MiB and must have an approved MIME type; binary
  PDF/image signatures are checked again by the Connector.
- Browser requests use fresh memory-only sessions which close after each
  operation. No profiles, cookies or user workspaces are shared between users.
- Browser containers have only internal Docker networks. A separate public-only
  proxy resolves and validates every connection, then connects to that exact IP.
  Private, loopback, link-local, metadata, special-use and mixed public/private
  DNS results are blocked. Only web ports are permitted. Local infrastructure
  domains are blocked too. Redirects and subresources pass through the same proxy.
- Navigation rejects failed HTTP responses. Page network interception blocks
  non-GET/HEAD requests, WebSockets, service workers and unnecessary media.
- The browser has no Nextcloud/provider credential, user workspace, host port or
  Docker socket. Connectors alone receive its dedicated per-user control tokens.
  Worker/model task grants cannot use browser or account control endpoints directly.

## Docker deployment

Keep the official Hermes runtime and existing models as already configured.
From the saved fork on the Docker host, build the sidecar and enable it only
for already-registered users:

```sh
python3 deploy/public_browser.py --root /opt/docker/hermes-talk --build --enable
python3 deploy/deploy_personal.py
```

The second command rebuilds the assistant's tool facade and restarts its normal
Docker services after checking for active work. The browser is a separate
`hermes-talk-web` Compose project under `web-browser/`; it publishes no port.
The existing tenant renderer preserves browser-network attachments on upgrades.
Newly added users require another explicit `--enable` run; no browser control
token is copied from an existing user.

Browser limit:768MiB,1CPU,256PIDs and384MiB temporary storage. Egress proxy
limit:96MiB,0.25CPU and64PIDs. The browser service is idle between operations;
Chromium runs only while a web operation is active. Chromium's own sandbox is
disabled in this deployment; Docker isolation,
read-only filesystems, dropped capabilities and credential separation remain.
Resource limits are ceilings, not measured idle or peak use.

`before-public-web-*` preserves original Connector settings and core Compose.
For rollback, wait for idle, restore only these backed-up settings and the
previous image references, recreate the assistant services, then stop the
separate web project. Keep user files, consent and saved artifacts intact.
