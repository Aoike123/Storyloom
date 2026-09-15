# Storyloom public demo deployment

This stack publishes Storyloom on one Linux server: Caddy, Next.js, FastAPI, the background worker
and PostgreSQL with persistent named volumes. Only ports 80 and 443 are published.

Generation is paid for in one of two ways, and the account dock in the top-right corner is the only
place that manages it:

- **Compute beans** — every visitor signs in with a Zhihu account and receives a one-time bean
  grant. Their calls spend that grant instead of the operator's budget, so one visitor cannot use
  up the day's allowance before anyone else arrives.
- **Own API keys** — accepts DeepSeek, SiliconFlow and MiniMax keys, and is the way forward once a
  wallet is empty. Provider endpoints and model identifiers stay fixed by the application.

There is no site password and no anonymous shared pool. Own-key credentials are encrypted with
`MODEL_ACCESS_SECRET`, retained for a short session, and tasks store only its opaque id. The bean
wallet is keyed by the Zhihu account id held as text, because `uid` is an int64 that JavaScript
cannot represent exactly.

This is intentionally a public demo, not a tenant-isolated SaaS platform. Works, progress and media
are shared across visitors. The stack includes per-IP request limits, a global active-task cap and
container resource ceilings, but no CAPTCHA, distributed abuse scoring or upstream DDoS protection.
Size `BEANS_INITIAL_GRANT` so the total across expected signups stays inside your provider budget.

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

There is no anonymous shared pool. An operator key is spent only through a signed-in account's bean
wallet, and the size of that wallet is what bounds the spend: `BEANS_INITIAL_GRANT` per account,
with `BEANS_LLM_COST`, `BEANS_IMAGE_COST` and `BEANS_VIDEO_COST_PER_SECOND` setting the price of
each call. Size the grant so the total across expected signups stays inside your provider budget.
An authentication, payment, or permission response (HTTP 401/402/403) from a provider pauses that
provider for the rest of the Shanghai day, and affected accounts are told to bring their own key;
the provider dashboards remain the source of truth for recharge amounts.

For a 2 vCPU / 2 GiB demo host, the supplied defaults allow 180 read requests
and 20 write requests per client IP per minute, and at most 32 queued, running,
or waiting generation tasks globally. `/api/health` is exempt. Adjust
`PUBLIC_READ_REQUESTS_PER_MINUTE`, `PUBLIC_WRITE_REQUESTS_PER_MINUTE`, and
`PUBLIC_MAX_ACTIVE_TASKS` in `.env.production` only after observing real usage.
The request limiter is deliberately local to the single API container; use a
CDN/WAF or shared rate-limit service before scaling to multiple API replicas.
Only the reverse-proxy network may set `X-Forwarded-For`
(`STORYLOOM_TRUSTED_PROXY`, default `172.28.0.0/16`, matching
`STORYLOOM_NETWORK_SUBNET`). Trusting every peer would let a visitor forge a new
client address and reset its own per-IP limit. If you change the Compose subnet,
change both values together.

The Compose file also caps memory, CPU, and process counts per container so one
runaway service is less likely to take down the host. These defaults are tuned
for the documented small server. Generated media still consumes the system disk
and outbound bandwidth, so monitor both and move media to object storage/CDN
before inviting sustained traffic.

## Zhihu account login

Signed-in visitors spend **compute beans** instead of the anonymous shared pool, so one visitor
cannot consume the whole day's budget. Anonymous visitors keep using the pool or their own keys.
The two paths are independent: beans come from the operator's keys, and a visitor whose beans run
out can switch to their own keys at `/<setup>` at any time.

### One-time setup

1. On the hackathon project page, obtain the App ID and App Key.
2. Register this exact callback on the Zhihu open platform — protocol, host, path and trailing
   slash must match character for character:

   ```text
   https://<SITE_ADDRESS>/auth/callback
   ```

3. Put the credentials in `.env.production` (never in the repository or an image layer):

   ```text
   ZHIHU_OAUTH_APP_ID=<app id>
   ZHIHU_OAUTH_APP_KEY=<app key>
   ZHIHU_OAUTH_REDIRECT_URI=https://<SITE_ADDRESS>/auth/callback
   ```

4. Redeploy. `GET /api/zhihu/status` must report `"configured": true`.
5. Open `/author` and click the login entry. **You** complete the Zhihu consent screen; the agent
   must not click it for you.

`/auth/callback` is deliberately outside `/api`, so the Caddyfile routes it to the API container.
If you change the public path, change both the registered URI and that route together.

### Beans

Each account receives `BEANS_INITIAL_GRANT` once, at first login. Costs are `BEANS_LLM_COST`
(default 1), `BEANS_IMAGE_COST` (default 5) and `BEANS_VIDEO_COST_PER_SECOND` (default 6). With the
supplied defaults one six-shot film costs roughly 408 beans, so the 500 default grant finishes a
film with a little room for a retry. Set `BEANS_INITIAL_GRANT=0` to make accounts bring their own
keys from the start.

Beans are debited when a call is actually submitted, not when a task is queued, and a provider that
refuses the request returns them. Set the costs to `0` to make the wallet a pure login gate.

### What is stored where

| Value | Where it lives | Never appears in |
| --- | --- | --- |
| App Key | deployment secret | repository, image, log, response |
| OAuth access token | server-side, encrypted with `MODEL_ACCESS_SECRET` | browser, URL, log, response |
| `uid`, nickname, avatar | server-side session record | — (nickname and avatar are shown to that account) |
| Browser session | `HttpOnly` cookie holding a random id | — |

The login screen may not be able to verify `state` if Zhihu does not return it. The app rejects a
mismatched, missing, expired or replayed `state`, so treat a real login that completes as
confirmation that the value came back; if it ever stops returning `state`, logins will fail closed
rather than degrade silently.

## Diagnosing a failed run

A failed task shows a short code such as `E-1A2B3C` in its progress message. On
the server (local mode) `GET /api/diagnostics/E-1A2B3C` returns the full
traceback, stage and non-secret context; in public demo mode that endpoint
returns 404 so visitors never receive stack traces or provider bodies. Failed
production nodes retry themselves once, reusing already-validated results, and
then wait for a person. See [制作流程的安全不变量](pipeline-safety.md) for the
full list of invariants.

If Docker Hub is unreachable from a mainland China server, set
`DOCKER_HUB_PREFIX=m.daocloud.io/docker.io/library/` in `.env.production`.
This changes only Storyloom's four base images and does not modify the Docker
daemon's global registry configuration.

## 3. Deploy

```bash
bash scripts/deploy.sh
```

### If the first deploy after a Compose change stops with "has active endpoints"

`scripts/deploy.sh` runs `up --remove-orphans`, which makes Docker recreate the default network
whenever its definition changes — for example the first release that pins
`STORYLOOM_NETWORK_SUBNET`. Containers still attached to the old network block that recreate, and
the run stops with:

```text
error while removing network: network storyloom_default has active endpoints
```

Stop the containers first, then deploy. Omitting `-v` keeps every named volume, so works, media and
the database are untouched:

```bash
docker compose --env-file .env.production -f compose.production.yaml down --remove-orphans
bash scripts/deploy.sh
# The Caddyfile is mounted, not reloaded: restart Caddy so a new public path takes effect.
docker compose --env-file .env.production -f compose.production.yaml restart caddy
```

Never add `-v` to that `down`: it deletes `postgres_data`, `story_data` and the Caddy volumes.

### Verify the Zhihu login after deploying

```bash
curl -s https://<SITE_ADDRESS>/api/zhihu/status
```

`"configured":true` means the App ID, App Key and redirect URI were all read. Then confirm that the
login actually sends the registered callback, which is the value the activity page must match
character for character:

```bash
curl -s -o /dev/null -w '%{redirect_url}\n' https://<SITE_ADDRESS>/api/zhihu/login
```

The decoded `redirect_uri` must equal the address registered on the activity page. A mismatch there
is the most common cause of a login that fails before the consent screen.

The script validates Compose and Caddy configuration, builds immutable
application images, starts services in dependency order, and waits for the
database, API, worker, and web health checks. It never removes data volumes.

Open `https://YOUR_DOMAIN`. The story catalog is public. Entering a story sends a visitor to Zhihu
to sign in and receive their bean grant, unless they would rather attach their own keys from the
account dock in the top-right corner. Useful checks:

```bash
docker compose --env-file .env.production -f compose.production.yaml ps
docker compose --env-file .env.production -f compose.production.yaml logs --tail=100 api worker web caddy
curl https://YOUR_DOMAIN/api/health
curl https://YOUR_DOMAIN/api/zhihu/status
curl https://YOUR_DOMAIN/api/model-access/status
```

A healthy response reports PostgreSQL and an online worker. `/api/zhihu/status` must report
`"configured": true` before the sign-in button can work.

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
