---
name: review-and-assemble
description: Review generated film takes against approved shot and asset constraints, record adoption and rejection decisions, assemble a non-destructive editorial timeline, run continuity and delivery checks, and prepare an export manifest. Use when generated images, video, or audio must be selected, annotated, ordered, trimmed, quality-checked, or packaged for editorial handoff.
---

# Review And Assemble

Make editorial decisions traceable and reversible. Never alter the source media to hide an unresolved production problem.

## Inputs

Read the shared request envelope and validate `payload` against `schemas/input.schema.json`.

Require generation output, an approved shot plan, and production assets. Accept prior review decisions and a timeline for incremental review.

## Workflow

1. Reconcile generated media with producing jobs, compiled requests, shot segments, and assets.
2. Verify that media can be read and that duration, aspect ratio, and basic metadata are available.
3. Evaluate each take for story coverage, identity, continuity, composition, action, technical quality, and policy constraints.
4. Record `selected`, `rejected`, `revision-needed`, or `unreviewed` without deleting media.
5. Select at most one primary take per shot unless the editorial plan explicitly requires alternatives.
6. Assemble selected takes into a non-destructive timeline with source in/out points.
7. Run cross-shot continuity and delivery checks.
8. Produce an export manifest only when all blocking checks pass.

## Output rules

Return:

- `skill: review-and-assemble`
- `data.reviews`
- `data.selected_takes`
- `data.timeline`
- `data.quality_control`
- `data.export_manifest`

Every review must reference a media asset and shot segment. Every timeline item must preserve source media ID and source time range. Rejections and requested revisions must include reason codes.

## Boundaries

- Do not delete or overwrite source media.
- Do not select a take that violates a blocking story, identity, or continuity constraint.
- Do not conceal missing shots with fabricated media.
- Do not claim final delivery readiness when a blocking QC item remains open.
- Do not perform irreversible edits.

Read [references/field-guide.md](references/field-guide.md) for review decisions, timeline rules, and blocking QC.
