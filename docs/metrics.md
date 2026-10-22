# Agentic-development metrics

This file defines the only three metrics kept for agentic development on this project, and holds the table they are
recorded in. It exists so that claims about how well the workflow works rest on recorded facts, not impressions.

## Metrics

- **CI first-pass** — yes or no. `yes` when the first CI run on the first pushed head of the pull request is green.
  Later runs, re-runs and fixup pushes do not change the value.
- **Review cycles** — a count. The number of review rounds that requested changes before the pull request was approved.
  A pull request approved at the first review has `0`.
- **Confirmed defects** — a count, with a link and a date for each. A defect in the pull request's code that was later
  confirmed by a human or by a failing test. A reviewer suspicion that was not confirmed does not count.

## Record

One row per pull request, entered by hand at merge. Confirmed defects can be found after merge; add them to the row
when they are confirmed, as `link (date)`.

| PR | CI first-pass | Review cycles | Confirmed defects |
|---|---|---|---|

No data yet; first row is the first PR that has CI. Earlier slices (S1–S4) are not back-filled.

## Not measured

These are deliberately not tracked. Revisit one only if a decision needs it.

- Tokens.
- Wall-clock time.
- Per-agent cost.
