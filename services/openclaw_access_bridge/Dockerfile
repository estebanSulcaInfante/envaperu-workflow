FROM node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts
COPY src ./src
RUN mkdir -p /var/lib/scm-openclaw-bridge && chown -R node:node /var/lib/scm-openclaw-bridge

USER node
ENV NODE_ENV=production SCM_BRIDGE_HOST=0.0.0.0 SCM_BRIDGE_PORT=5182
VOLUME ["/var/lib/scm-openclaw-bridge"]
EXPOSE 5182
CMD ["node", "src/server.js"]
