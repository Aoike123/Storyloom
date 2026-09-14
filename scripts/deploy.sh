#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${STORYLOOM_DEPLOY_ENV:-${repo_root}/.env.production}"
compose_file="${repo_root}/compose.production.yaml"

if [[ ! -f "${env_file}" ]]; then
  echo "Missing ${env_file}. Copy .env.production.example and fill in the real values." >&2
  exit 1
fi
if grep -Eq 'storyloom\.example\.com|replace-with-' "${env_file}"; then
  echo "Deployment environment still contains example placeholders." >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker Engine with the Compose plugin is required." >&2
  exit 1
fi

cd "${repo_root}"
compose=(docker compose --env-file "${env_file}" --file "${compose_file}")

"${compose[@]}" config --quiet
"${compose[@]}" run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
"${compose[@]}" up --detach --build --remove-orphans --wait
"${compose[@]}" ps

echo "Storyloom deployment is healthy. Caddy will finish HTTPS provisioning after DNS reaches this server."
