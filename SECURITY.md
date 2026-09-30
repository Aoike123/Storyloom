# Security policy

## Supported version

Security fixes are applied to the latest revision of the default branch. This
repository is a pure local workbench that binds to the loopback interface; it
is not a multi-user service.

## Reporting a vulnerability

Please use GitHub private vulnerability reporting when it is enabled for this
repository. If that channel is unavailable, contact the repository maintainer
privately through the repository owner's profile. Do not publish credentials,
private story text, provider responses, signed media URLs, or exploit details
in a public issue.

## Deployment boundary

The workbench intentionally has no account, session, or tenant isolation: all
works, assets, and generation records belong to this machine's single operator.
Model keys live only in the local settings file (`.env.local`, or the file
named by `STORYLOOM_ENV_FILE`) and are never returned to the browser in full.
The services bind to `127.0.0.1` only. Do not publish ports 3000 or 8000,
point a reverse proxy at them, or otherwise expose the workbench beyond the
local machine.

Because the workbench is loopback-only, its threat surface is whoever can
already reach this machine; it deliberately ships no remote-abuse controls.
Before exposing it as a networked or multi-user service you must add HTTPS,
real account and session isolation, per-visitor ownership, abuse and rate
limiting, and content moderation.

Never commit `.env.local`, files below `data/`, application logs, database
files, uploaded media, or real provider credentials. If a credential is ever
committed, revoke and rotate it; removing it in a later commit is not enough.
