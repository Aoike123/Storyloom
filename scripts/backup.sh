#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${STORYLOOM_DEPLOY_ENV:-${repo_root}/.env.production}"
compose_file="${repo_root}/compose.production.yaml"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="${1:-${repo_root}/backups/${timestamp}}"

mkdir -p "${target}"
cd "${repo_root}"
compose=(docker compose --env-file "${env_file}" --file "${compose_file}")
"${compose[@]}" config --quiet

"${compose[@]}" exec -T db pg_dump --username storyloom --dbname storyloom --format=custom > "${target}/database.dump"
"${compose[@]}" exec -T api python -c 'import sys,tarfile; archive=tarfile.open(fileobj=sys.stdout.buffer,mode="w|gz"); archive.add("/app/data",arcname="data"); archive.close()' > "${target}/story-data.tar.gz"

if command -v sha256sum >/dev/null 2>&1; then
  (cd "${target}" && sha256sum database.dump story-data.tar.gz > SHA256SUMS)
fi

echo "Backup created at ${target}. It contains provider settings and must be stored as a secret."
