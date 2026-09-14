# Field Guide

## IDs

- Plan: `shp-001`
- Segment: `seg-001`
- Spatial baseline: `spc-001`

Preserve segment IDs during targeted retries.

## Duration policy

Use one policy per plan:

- `exact`: segment total must equal target duration.
- `bounded`: segment total must remain inside minimum and maximum.
- `editorial`: duration is advisory and must include a reason.

## Blocking preflight issues

- missing beat
- non-positive duration
- unresolved critical asset
- contradictory start and end state
- impossible camera and performer path
- axis or screen-direction contradiction without an intentional transition
