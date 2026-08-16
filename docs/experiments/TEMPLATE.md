# E<NN> — <one-line question this experiment answers>

> Copy this file to `E<NN>-<slug>.md` and fill it in. One experiment per file.
> An experiment that produced a negative result is a **successful** experiment and must be
> written up with the same care as a positive one — the whole point is that this project
> has empirical parts, and the record of what does *not* work is what stops the next agent
> from re-deriving it.

- **Status:** Not started | In progress | Concluded
- **Resolves risk:** R<n> (from `PLAN.md` §10)
- **Gates milestone:** M<n>
- **Owner / date:**

## Question

State it so that it has a yes/no or numeric answer. "Does X work?" is too vague;
"do SCO data packets cross the HCI transport in both directions during a live call?" is not.

## Why it cannot be answered by reading

One or two sentences. If it *can* be answered from documentation or source, do that
instead and record the citation — do not run an experiment to avoid reading.

## Method

- Script: `tests/.../<script>.sh`
- Hardware present:
- Conditions held constant:
- Conditions varied (one per run):

## Runs

| # | Date | Variant / conditions | Artifact dir | Verdict |
|---|---|---|---|---|
| 1 | | | `artifacts/...` | |
| 2 | | | | |

## Raw data

Copy the artifact directories that matter into `docs/experiments/results/E<NN>/`.
Commit btmon captures, WAVs and `analysis.txt`. **Do not summarise away the evidence** —
the numbers in this file must be re-derivable from what is committed next to it.

## Result

The measurement, with units. Tables and numbers, not adjectives.

## Verdict

One of: CONFIRMED / REFUTED / INCONCLUSIVE — and what that means for the plan.

## Consequences for the plan

- Risk R<n> probability moves from <x> to <y>
- ADR-<nnnn> is / is not affected
- Milestone M<n> proceeds / changes / is blocked
- `PLAN.md` sections needing an edit:

## Follow-up questions this raised

Things that were not knowable before running it. These often become the next experiment.
