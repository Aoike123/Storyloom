---
name: structure-screenplay
description: Convert screenplays, treatments, scene drafts, or narrative prose into traceable scenes, units, beats, rhythm data, and production facts. Use when a film-production workflow needs structured story evidence, resumable breakdown stages, or a reliable upstream artifact for story planning, asset design, and shot planning.
---

# Structure Screenplay

Convert source text into production-oriented structure without changing story facts.

## Inputs

Read the shared request envelope and validate `payload` against `schemas/input.schema.json`.

Require:

- `payload.script_text`
- `project.id`

Treat project metadata, target scope, and prior checkpoints as constraints, not story evidence.

## Workflow

1. Preserve the supplied text as the authoritative source.
2. Split it into scenes using explicit headings when available and conservative inference otherwise.
3. Split each scene into narrative units when location, time, objective, or dramatic condition changes.
4. Describe the rhythm curve from observable changes in pressure, pace, emotion, and information.
5. Segment units into beats with stable IDs and source ranges.
6. Add production facts for characters, locations, props, dialogue, action, sound, and continuity.
7. Record uncertain interpretation as diagnostics instead of silently completing missing facts.
8. Preserve completed stages when later work fails or a retry targets only selected IDs.

## Output rules

Return the shared result envelope with:

- `skill: structure-screenplay`
- `data.scenes`
- `data.units`
- `data.beats`
- `data.rhythm_curve`
- `data.progress`

Every unit and beat must include a stable ID and at least one source reference. Never discard valid earlier-stage output because a later stage is partial.

Use `completed` only when every requested stage is complete. Use `partial` when useful structure exists but detail is incomplete. Use `blocked` when required source text is absent.

## Boundaries

- Do not rewrite dialogue or invent events.
- Do not infer visual design details that are not needed for structural clarity.
- Do not create shot choices.
- Do not merge distinct characters or locations solely because their names are similar.

Read [references/field-guide.md](references/field-guide.md) when assigning IDs, source ranges, or production facts.
