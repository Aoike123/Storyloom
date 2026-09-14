# Field Guide

## Asset IDs

- Character: `chr-001`
- Location: `loc-001`
- Prop: `prp-001`
- Variant: append a stable suffix such as `chr-001-v02`

## Identity anchors

Identity anchors are properties that should remain stable across shots. Allowed variation records controlled changes. Do not place the same property in both lists.

## Coverage

Report:

- referenced upstream IDs
- covered upstream IDs
- missing assets
- ambiguous identity matches
- unused assets

A missing critical asset blocks downstream readiness; a missing optional background asset produces a warning.
