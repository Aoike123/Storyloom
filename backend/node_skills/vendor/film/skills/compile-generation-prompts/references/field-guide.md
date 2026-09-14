# Field Guide

## Image request order

1. subject identity
2. environment and spatial anchors
3. framing and camera
4. frozen action state
5. lighting and material
6. continuity locks

## Video request order

1. start state
2. dominant action progression
3. camera motion
4. performance change
5. end state
6. continuity constraints

## Quality gate

Block a request when:

- a required shot or critical asset is missing
- duration is invalid
- start and end states conflict
- the selected generation mode is unsupported
- provider parameters are outside the capability profile

Use warnings for verbosity, weak reference roles, excessive negative constraints, and optional continuity omissions.

## Provider capability matching

Treat a capability profile as a contract, not a model advertisement.

For every provider request:

1. identify the media type;
2. choose the required generation mode;
3. assign reference roles explicitly;
4. check first frame, last frame, multi-reference, character reference, audio,
   duration, aspect ratio, and maximum reference count;
5. include only supported parameters;
6. return one issue per mismatch with safe alternatives.

Never convert a last-frame requirement into an ordinary reference image or
discard extra references without reporting the semantic change.
