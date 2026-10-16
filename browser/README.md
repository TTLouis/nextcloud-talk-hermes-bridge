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

Assistant-drafted searches first show the exact outgoing query as a reaction
choice, including in new tasks. Choose whether to send it. The approval
is bound to that exact request and task; the model cannot approve it itself.
An exact query you provide as `搜索：your query` or `search: your query` can run
directly. This is optional; ordinary requests use the choice card. Authentication
material is blocked outright, including common URL/base64 encodings, and an owner
refusal prevents rewording the request to keep asking during that task.

The assistant can follow exact public links returned by its web tools or URLs
in your current native Talk message. A model-created URL containing additional
text requires a separate choice. Model-authored handoffs do not count as a
user-provided URL. This prevents silently appending private text to outgoing
URLs after reading an injected page. It is not a proof that an LLM will never
follow misleading information; factual reliability and prompt injection remain
separate concerns.

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
