# Security policy

## Supported version

Security fixes are applied to the latest revision of the default branch. This
repository is currently a local demonstration prototype, not a hardened
multi-user service.

## Reporting a vulnerability

Please use GitHub private vulnerability reporting when it is enabled for this
repository. If that channel is unavailable, contact the repository maintainer
privately through the repository owner's profile. Do not publish credentials,
private story text, provider responses, signed media URLs, or exploit details
in a public issue.

## Deployment boundary

The current demo intentionally has no account or tenant isolation. Do not
expose it directly to the public internet. A hosted demonstration must at
minimum be protected by HTTPS and an external access-control layer, keep paid
model calls disabled by default, and prevent visitors from editing provider
configuration.

Never commit `.env.local`, files below `data/`, application logs, database
files, uploaded media, or real provider credentials. If a credential is ever
committed, revoke and rotate it; removing it in a later commit is not enough.
