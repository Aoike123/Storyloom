#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${STORYLOOM_DEPLOY_ENV:-${repo_root}/.env.production}"
compose_file="${repo_root}/compose.production.yaml"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_target="${repo_root}/backups/pre-reset-${timestamp}"

if [[ "${1:-}" != "--yes" || "$#" -ne 1 ]]; then
  cat >&2 <<'EOF'
This resets the complete production application state.

It deletes every account session, wallet, work, task, public release, application cache and
generated media file. Deployment secrets, provider settings and Caddy HTTPS certificates are
preserved. A private backup is written before anything is deleted.

Run again with: bash scripts/reset-production.sh --yes
EOF
  exit 2
fi
if [[ ! -f "${env_file}" ]]; then
  echo "Missing ${env_file}." >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker Engine with the Compose plugin is required." >&2
  exit 1
fi

cd "${repo_root}"
compose=(docker compose --env-file "${env_file}" --file "${compose_file}")
"${compose[@]}" config --quiet

mkdir -p "${backup_target}"
trap 'echo "Production reset stopped after an error. Services may still be offline; keep the backup at ${backup_target}." >&2' ERR

echo "Stopping public traffic and all application writers..."
"${compose[@]}" stop caddy web api worker

echo "Backing up PostgreSQL and the application data volume..."
"${compose[@]}" exec -T db pg_dump --username storyloom --dbname storyloom --format=custom > "${backup_target}/database.dump"
"${compose[@]}" run --rm --no-deps -T --entrypoint python api -c '
import sys, tarfile
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
    archive.add("/app/data", arcname="data")
' > "${backup_target}/story-data.tar.gz"
if command -v sha256sum >/dev/null 2>&1; then
  (cd "${backup_target}" && sha256sum database.dump story-data.tar.gz > SHA256SUMS)
fi

echo "Clearing all application rows..."
"${compose[@]}" exec -T db psql --username storyloom --dbname storyloom --set ON_ERROR_STOP=1 \
  --command 'TRUNCATE TABLE tasks, records RESTART IDENTITY CASCADE;'

echo "Clearing generated media and server-side application caches..."
# Keep the service account here. The hardened service drops every root capability, while the
# persistent volume is intentionally owned by the Storyloom service user that created the files.
"${compose[@]}" run --rm --no-deps -T --entrypoint python api -c '
from pathlib import Path
import shutil

root = Path("/app/data").resolve()
for name in ("media", "manifests", "staging", "logs"):
    target = (root / name).resolve()
    if target.parent != root:
        raise RuntimeError(f"refusing unsafe target: {target}")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink(missing_ok=True)
for name in ("processes.json", "provider-models.json"):
    (root / name).unlink(missing_ok=True)
for target in root.glob("story.db*"):
    resolved = target.resolve()
    if resolved.parent != root or not resolved.is_file():
        raise RuntimeError(f"refusing unsafe target: {resolved}")
    resolved.unlink()
'

echo "Discarding old application containers and rebuilding without the frontend/backend build cache..."
"${compose[@]}" rm --force caddy web api worker
"${compose[@]}" build --no-cache api web
"${compose[@]}" up --detach --force-recreate --remove-orphans --wait
"${compose[@]}" ps

trap - ERR
echo "Production reset is complete. Backup: ${backup_target}"
echo "All users must sign in again; the next login receives a fresh account state."
