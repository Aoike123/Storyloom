# Field Guide

## Review decisions

- `selected`: approved as the primary or explicitly named alternate take.
- `rejected`: unsuitable without regeneration; include one or more reason codes.
- `revision-needed`: potentially usable after a defined correction.
- `unreviewed`: no human or authorized automated decision has been made.

Do not infer approval from file existence.

## Timeline

- Preserve media IDs and original source in/out time.
- Keep edits non-destructive.
- Prevent negative duration and source ranges outside known media duration.
- Preserve shot-plan order unless an editorial change includes an explicit reason.
- Represent gaps explicitly instead of silently closing missing coverage.

## Blocking quality checks

- missing required shot
- unreadable or unpersisted media
- critical character identity mismatch
- story action contradicts the approved segment
- impossible source time range
- unresolved content-policy restriction
- export setting incompatible with the project aspect ratio or required delivery format
