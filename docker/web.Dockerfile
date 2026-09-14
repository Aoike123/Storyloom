ARG DOCKER_HUB_PREFIX=
FROM ${DOCKER_HUB_PREFIX}node:22-bookworm-slim AS build

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

RUN npm install --global pnpm@11.22.0
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build \
    && pnpm prune --prod

FROM ${DOCKER_HUB_PREFIX}node:22-bookworm-slim AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

RUN groupadd --gid 10001 storyloom \
    && useradd --uid 10001 --gid storyloom --no-create-home --home-dir /app --shell /usr/sbin/nologin storyloom

COPY --from=build --chown=storyloom:storyloom /app/package.json ./package.json
COPY --from=build --chown=storyloom:storyloom /app/node_modules ./node_modules
COPY --from=build --chown=storyloom:storyloom /app/.next ./.next

USER storyloom
EXPOSE 3000
STOPSIGNAL SIGTERM

CMD ["node", "node_modules/next/dist/bin/next", "start", "--hostname", "0.0.0.0", "--port", "3000"]
