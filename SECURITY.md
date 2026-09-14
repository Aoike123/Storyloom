# Security policy

## Supported version

Security fixes are applied to the latest revision of the default branch. This
repository includes an anonymous public-demo deployment, but it is not a
hardened multi-user service.

## Reporting a vulnerability

Please use GitHub private vulnerability reporting when it is enabled for this
repository. If that channel is unavailable, contact the repository maintainer
privately through the repository owner's profile. Do not publish credentials,
private story text, provider responses, signed media URLs, or exploit details
in a public issue.

## Deployment boundary

The current demo intentionally has no account or tenant isolation. Its supplied
public stack terminates HTTPS at Caddy, fixes provider transports and model ids
in application code, encrypts short-lived BYOK sessions, and places operator
keys behind a daily reservation budget. Do not publish ports 3000 or 8000
directly, bypass the reverse proxy, or run a public pool without a conservative
`PUBLIC_POOL_DAILY_BUDGET_CNY`.

The anonymous workspace is shared: visitors can see and change common works.
There is no CAPTCHA, per-IP throttling, abuse detection, or content moderation
boundary yet. Add those controls and tenant isolation before treating this as a
general production service.

Never commit `.env.local`, files below `data/`, application logs, database
files, uploaded media, or real provider credentials. If a credential is ever
committed, revoke and rotate it; removing it in a later commit is not enough.
