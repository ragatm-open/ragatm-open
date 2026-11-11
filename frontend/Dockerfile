# frontend/Dockerfile
#
# image de base épinglée par digest ; l'image finale n'embarque que les dépendances de
# PRODUCTION (`npm ci --omit=dev`), les outils de test/build (vitest, vite, esbuild…) n'y sont plus présents.
# Qualification : base `node:22-alpine` (Node 22 LTS, Alpine 3.24) — liste de manifestes
# multi-arch relevée le 2026-11-09. `node:20-alpine` (Node 20, fin de vie depuis le 2026-04-30) portait
# au scan trivy 1 avis CRITIQUE (node-tar du npm embarqué) et 28 avis ÉLEVÉS (libssl/libcrypto, musl,
# zlib) sans rebuild disponible ; Next 15.5 exige Node ≥ 18.18. Validation : `npm ci` + `next build` +
# `next start` + contrat du proxy rejoués dans un conteneur `node:22-alpine` (evidence/targeted-repair/next).
FROM node:22-alpine@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202 AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-alpine@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202 AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202 AS proddeps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

FROM node:22-alpine@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202 AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Next start a besoin du build + node_modules (production uniquement) + public
COPY --chown=node:node --from=builder /app/.next ./.next
COPY --chown=node:node --from=builder /app/public ./public
COPY --chown=node:node --from=builder /app/package.json ./package.json
COPY --chown=node:node --from=proddeps /app/node_modules ./node_modules

# Provenance : la révision source, passée par le compose (`--build-arg`), posée en
# label OCI. Sans elle, une image frontend ne dit pas de quel code elle vient, et
# `scripts/deploy/assert-revision.sh` la refuse. Métadonnée seule : le contenu et
# l'exécution de l'image n'en dépendent pas. Déclarée en dernier pour ne pas
# invalider le cache des étapes précédentes.
ARG APP_GIT_SHA=""
LABEL org.opencontainers.image.revision=$APP_GIT_SHA

# Surface d'exécution réduite : npm, npx, yarn et corepack ne servent qu'au build ; ils sont retirés de l'image
# finale (leurs dépendances embarquées portaient les seuls avis de sécurité de l'image). Le serveur Next est lancé
# directement par Node. Exécution sous l'utilisateur non privilégié `node` (UID 1000) de l'image de base.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack /opt/yarn* \
    /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/yarn /usr/local/bin/yarnpkg /usr/local/bin/corepack
USER node
EXPOSE 3000
CMD ["node", "node_modules/next/dist/bin/next", "start"]
