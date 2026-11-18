# Web runner: the standard runner plus Playwright (lockfile-pinned) and Chromium.
FROM swfactory/runner:0.1.0
USER root
COPY docker/runner-web/package.json docker/runner-web/package-lock.json /opt/playwright/
RUN cd /opt/playwright && npm ci --omit=dev \
 && PLAYWRIGHT_BROWSERS_PATH=/ms-playwright /opt/playwright/node_modules/.bin/playwright install --with-deps chromium \
 && chmod -R a+rX /opt/playwright /ms-playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright NODE_PATH=/opt/playwright/node_modules
USER 10001
