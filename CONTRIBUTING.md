# Contributing to Storyloom

Thank you for helping improve Storyloom.

## Development setup

Follow the quick-start instructions in `README.md`. Keep `ALLOW_PAID_CALLS=false`
for development and tests unless you are deliberately running an authorized
provider integration test with an agreed budget.

Before submitting a change, run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
pnpm --dir web test
pnpm --dir web build
```

## Pull requests

- Keep each change focused and explain the user-visible behavior it affects.
- Add or update tests for behavior changes.
- Preserve task versioning, provider idempotency, and the rule that uncertain
  paid submissions are not retried automatically.
- Do not include local databases, logs, uploaded media, model outputs, real API
  responses, or credentials.
- Do not add story text or media unless its redistribution and adaptation rights
  are documented.
- Keep third-party vendor licenses and update
  `backend/node_skills/vendor-lock.json` when refreshing vendored files.

By submitting a contribution, you agree that it is licensed under the
Apache License 2.0 used by this repository.
