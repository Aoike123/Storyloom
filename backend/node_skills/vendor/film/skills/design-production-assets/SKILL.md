---
name: design-production-assets
description: Define reusable character, location, and prop assets from structured story evidence and approved creative constraints. Use when a film project needs stable asset identities, variants, visual briefs, continuity locks, coverage checks, or traceable references for shot and generation workflows.
---

# Design Production Assets

Create reusable production asset specifications, not generated media.

## Inputs

Read the shared request envelope and validate `payload` against `schemas/input.schema.json`.

Accept structured screenplay data, a story blueprint, or both. If only one is present, continue when useful evidence exists and set status to `partial`.

## Workflow

1. Audit upstream status and distinguish facts from creative proposals.
2. Inventory characters, locations, and props referenced by the story.
3. Merge aliases only when identity evidence is strong.
4. Assign stable asset IDs and semantic roles.
5. Define identity anchors, continuity locks, allowed variation, and unresolved details.
6. Add variants only when required by story state, time, costume, damage, or production need.
7. Write provider-neutral visual briefs.
8. Check coverage against upstream story nodes and beats.

## Output rules

Return:

- `skill: design-production-assets`
- `data.characters`
- `data.locations`
- `data.props`
- `data.coverage`

Every asset must include evidence references. Every variant must reference its parent asset and explain why it exists.

Use `partial` when missing upstream evidence limits coverage or when critical project constraints remain unknown.

## Boundaries

- Do not call image-generation services.
- Do not turn temporary composition notes into permanent identity traits.
- Do not invent biographies, costume changes, or prop states without evidence or explicit proposal labels.
- Do not create duplicate assets for repeated mentions.

Read [references/field-guide.md](references/field-guide.md) for asset identity and variation rules.
