# Proposals

A proposal records a design change before code lands: anything that alters an invariant,
a public name, a model, a setting, a check, the protocol or the client.

## Format

File name: `NNNN-short-title.md`, numbered in order of creation.

```markdown
# Proposal NNNN: <title>

Status: draft | accepted | rejected | superseded by NNNN

## Problem
What is wrong or missing, with evidence.

## Proposal
The change, precisely enough to implement.

## Alternatives
What else was considered and why not.

## Spec changes
The sections of SPEC.md this updates, and how.
```

An accepted proposal is folded into `SPEC.md` in the same change that implements it.
