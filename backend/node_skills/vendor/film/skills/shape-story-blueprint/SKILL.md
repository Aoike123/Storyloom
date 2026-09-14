---
name: shape-story-blueprint
description: Transform structured screenplay evidence into a production creative brief, narrative nodes, continuity constraints, and downstream handoff data. Use when a film project needs a compact story blueprint, configurable narrative board, or traceable planning layer before asset and shot design.
---

# Shape Story Blueprint

Create a planning layer that is concise enough for production use and traceable enough for review.

## Inputs

Read the shared request envelope and validate `payload` against `schemas/input.schema.json`.

Require a `screenplay_structure` artifact produced from source evidence. Accept project-wide creative constraints when they are explicitly supplied.

## Workflow

1. Audit upstream status, IDs, and source references.
2. Separate known project constraints from unresolved creative choices.
3. Build a creative brief covering format, genre, tone, visual medium, aspect ratio, audience, and style constraints.
4. Select narrative nodes that represent meaningful changes in situation, objective, pressure, or outcome.
5. Map every narrative node to upstream scene, unit, or beat IDs.
6. Build a configurable planning board only when requested; do not force a fixed story template.
7. Extract continuity notes and production handoff requirements.
8. Mark unsupported interpretation as a proposal, never as source fact.

## Output rules

Return:

- `skill: shape-story-blueprint`
- `data.creative_brief`
- `data.story_nodes`
- `data.planning_board`
- `data.continuity_notes`

Use `partial` when core story nodes are useful but required project constraints remain unknown. Put those unknowns in diagnostics and `handoff.blocked_by`.

## Boundaries

- Do not force the story into a named dramatic formula.
- Do not alter events to make the board symmetrical.
- Do not treat mood, composition, or design proposals as screenplay facts.
- Do not create final assets or shots.

Read [references/field-guide.md](references/field-guide.md) when selecting nodes or classifying project constraints.
