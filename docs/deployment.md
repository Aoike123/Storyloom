# Storyloom public demo deployment

This stack publishes Storyloom as an anonymous public demo on one Linux server.
It packages Caddy, Next.js, FastAPI, the background worker, and PostgreSQL with
persistent named volumes. Only ports 80 and 443 are published.

Visitors first open the model-access page and choose one of two modes:

- **Shared pool** — uses the operator's three API keys while each key's daily
  internal budget and provider check are available. Each provider is metered
  independently, and reservations are first come, first served.
- **Bring your own keys** — accepts only DeepSeek, SiliconFlow, and MiniMax keys.
  Provider endpoints and model identifiers are fixed by the application.

There is no site password and no product account. BYOK credentials are encrypted
with `MODEL_ACCESS_SECRET`, retained for a short anonymous session, and selected
tasks store only its opaque id. Switching modes affects newly submitted tasks;
already-running tasks keep the session with which they were created.

This is intentionally a public demo, not a tenant-isolated SaaS platform. Works,
progress, and media are shared across visitors. The first version has no CAPTCHA,
per-IP throttling, or abuse scoring, so keep the daily pool budget conservative.

## 1. Prepare the server and DNS

Use a supported Linux distribution and install Docker Engine plus the Docker
Compose plugin from the [official Docker instructions](https://docs.docker.com/engine/install/).
Create a non-root deployment user with permission to use Docker.

Create an `A` record from the intended hostname (for example,
`demo.example.com`) to the server IPv4 address. Add an `AAAA` record only if
IPv6 is configured on the server. Allow inbound TCP 22, 80, and 443, plus UDP
443; do not expose ports 3000, 5432, or 8000.

Caddy obtains and renews HTTPS certificates after DNS points at the server and
ports 80/443 are reachable. Its certificate state lives in a named volume.

## 2. Configure secrets and the shared pool

Clone the repository, then create the production environment file:

```bash
cp .env.production.example .env.production
chmod 600 .env.production
openssl rand -hex 32
openssl rand -hex 32
```

Use the two independent generated values for `POSTGRES_PASSWORD` and
`MODEL_ACCESS_SECRET`. Set `SITE_ADDRESS` to the bare hostname without a scheme
or path. Do not commit `.env.production`.

Add the operator-funded keys:

- `LLM_API_KEY`: DeepSeek
- `IMAGE_API_KEY`: SiliconFlow
- `VIDEO_API_KEY`: MiniMax China

The public deployment locks their transports and models to the product-tested
combination in `backend/environment.py`; visitors cannot submit alternate URLs
or model ids.

Set `PUBLIC_POOL_LLM_DAILY_BUDGET_CNY`,
`PUBLIC_POOL_IMAGE_DAILY_BUDGET_CNY`, and
`PUBLIC_POOL_VIDEO_DAILY_BUDGET_CNY` to the maximum amounts Storyloom may
reserve from the corresponding operator key each Shanghai calendar day. The
three `PUBLIC_POOL_*_RESERVE_CNY` values are conservative per-submission
estimates, not provider invoices. Every attempted provider submission keeps its
reservation, even if the provider later rejects it, so each local cap fails
closed rather than overspending from that key.

DeepSeek exposes an official balance endpoint, so the pool verifies that account
before it can be selected. SiliconFlow retired its `/user/info` balance endpoint
on 2026-08-14 and MiniMax does not currently document an equivalent general
balance API. Those two providers are guarded by their independent daily
reservation caps; an authentication, payment, or permission response (HTTP
401/402/403) disables only that provider's shared key for the remainder of that
Shanghai day. Their dashboards remain the source of truth for recharge amounts.

Set `PUBLIC_POOL_ENABLED=false` at any time to offer BYOK only.

If Docker Hub is unreachable from a mainland China server, set
`DOCKER_HUB_PREFIX=m.daocloud.io/docker.io/library/` in `.env.production`.
This changes only Storyloom's four base images and does not modify the Docker
daemon's global registry configuration.

## 3. Deploy

```bash
bash scripts/deploy.sh
```

The script validates Compose and Caddy configuration, builds immutable
application images, starts services in dependency order, and waits for the
database, API, worker, and web health checks. It never removes data volumes.

Open `https://YOUR_DOMAIN`. The story catalog is public; entering production
first opens `/setup`, where a visitor chooses an available shared pool or enters
their own three keys. Useful checks:

```bash
docker compose --env-file .env.production -f compose.production.yaml ps
docker compose --env-file .env.production -f compose.production.yaml logs --tail=100 api worker web caddy
curl https://YOUR_DOMAIN/api/health
curl https://YOUR_DOMAIN/api/model-access/status
```

A healthy response reports PostgreSQL and an online worker. The model-access
status must report `pool.available: true` before the shared option is selectable.

## Updates and rollback

Deploy a reviewed revision with:

```bash
git pull --ff-only
bash scripts/deploy.sh
```

For a rollback, check out a previously tested tag or commit and run the same
deployment script. Database schema changes currently use SQLAlchemy `create_all`;
introduce an explicit migration tool before making destructive or non-additive
schema changes.

## Backups

Run backups only when no media-generation job is writing files:

```bash
bash scripts/backup.sh
```

The timestamped backup contains a PostgreSQL custom-format dump, the entire
application data volume, and checksums. A database backup can contain encrypted,
not-yet-expired BYOK sessions. Store backups privately and never place
`MODEL_ACCESS_SECRET` in the backup directory. Restore is deliberately not
automated because it overwrites live data; rehearse a restore runbook before
accepting irreplaceable content.

## Container releases

`.github/workflows/containers.yml` builds both images for pull requests. When a
GitHub Release is published, it also publishes multi-architecture images to
GitHub Container Registry. The Compose deployment builds from checked-out source
by default, so it does not depend on registry visibility or credentials.
