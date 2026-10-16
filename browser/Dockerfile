ARG NODE_BASE=node@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6
FROM ${NODE_BASE}
WORKDIR /opt/browser
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright PLAYWRIGHT_CLI_SKIP_BROWSER_DOWNLOAD=1
RUN node node_modules/playwright/cli.js install --with-deps chromium --only-shell
COPY browser_service.mjs /opt/browser/browser_service.mjs
ENV NODE_ENV=production HOME=/tmp/browser-home NODE_OPTIONS=--max-old-space-size=192
USER 1000:1000
ENTRYPOINT ["node","/opt/browser/browser_service.mjs"]
