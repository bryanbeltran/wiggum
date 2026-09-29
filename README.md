# Wiggum

Wiggum is a standalone harness for autonomous maintenance and product iteration
against application repositories. It keeps project rules in each target repo
and keeps reusable prompts, policies, orchestration, state, and evidence here.

## What is implemented

- Modes: `bugfix`, `harden`, `feature`, `polish`, and `directed`.
- One cycle by default, configurable finite runs, indefinite runs, and graceful
  stop or pivot requests between atomic cycles.
- Read-only investigation, isolated Git worktrees, bounded implementation and
  adversarial review, configured validation, semantic commits, and fast-forward
  integration onto a persistent branch.
- Green/yellow/red risk handling, duplicate/backlog checks, no-change evidence,
  cycle journals, decision summaries, and periodic self-analysis eligibility.
- External per-project state with private permissions, common-secret redaction,
  raw Codex JSONL event capture, and screenshot hooks for configured browser
  workflows.
- A batch gate that stops for review. Wiggum does not push, force-push, or open
  pull requests automatically.

The first version uses TOML instead of YAML so configuration works with the
Python standard library and adds no runtime dependencies. Integrations for
issues, CI, publishing, and browser automation are configured per project rather
than assumed.

## Requirements

- Python 3.11 or newer.
- Git.
- Codex CLI on `PATH`, authenticated for the chosen model.
- A target Git repository with a persistent integration branch separate from
  its protected base branch.
- By default, `gpt-6-luna` with `max` reasoning effort; both are configurable.

## Install

```sh
python3 -m pip install -e .
```

Create a persistent integration branch in the target repository and leave the
checkout clean on it:

```sh
git switch -c feature/wiggum
```

Copy `config/projects.example.toml` to `config/projects.toml`, then set the target
repository path, integration branch, base branch, and project checks. Add a
product constitution in the target repo and point `constitution` to it.

```sh
wiggum config validate
wiggum harden --project garden --cycles 1
wiggum feature --project garden --selection council
wiggum directed --project garden "Improve saved-plan comparison"
```

`bugfix` and `directed` require an objective. FEATURE defaults to automatic
selection at the `product_minded` adventurousness level. Use `--selection council`
to record and print a weighted scorecard without implementing a candidate.

```sh
wiggum harden --project garden --forever
wiggum stop --project garden
wiggum pivot polish --project garden
wiggum pivot directed --project garden --objective "Improve saved-plan comparison"
```

An indefinite run finishes the active cycle before honoring stop or pivot.
Repeated Ctrl-C is not needed; use the `stop` command from another terminal.

## Git and review boundaries

Wiggum requires the target checkout to be clean and already on its configured
integration branch. It never switches the user's checkout automatically. Each
cycle runs in a detached worktree based on the integration SHA. Wiggum runs
configured checks against the base first, asks for an independent read-only
review after implementation, commits only reviewed changes, records a known-good
checkpoint, then fast-forwards the integration branch. The integration batch
defaults to a review gate after fifteen successful commits.

Large work may proceed when the investigator marks its design as confident and
validation passes. Yellow-risk work is still isolated in its cycle worktree.
Destructive migrations, authentication or authorization changes, secrets,
payments, privacy or legal implications, breaking APIs, and consequential
product choices are deferred for human approval.

## State and evidence

By default, Wiggum stores per-project state below
`$XDG_STATE_HOME/wiggum/projects/<alias>` or `~/.local/state/wiggum/projects/<alias>`.
The directory and files are private to the current user. Each cycle stores the
redacted Codex JSONL stream, stderr, prompts, investigation and review results,
validation logs, diffs, screenshots when configured, a rationale summary, and
rollback SHAs. `backlog.json`, `decisions.json`, and `iterations.jsonl` provide
queryable state. Raw model reasoning is neither requested nor required; Wiggum
retains observable events and concise rationale summaries.

Common API-key, bearer-token, and credential assignments are redacted. This is a
best-effort filter, not a guarantee that arbitrary sensitive content is removed.
Review evidence retention and state-directory access before sharing artifacts.
Each cycle is limited to four hours by default, agent output is capped, and the
runner stops when its evidence directory reaches the configured 100 MB limit.
Archive or remove old evidence before resuming after the evidence cap is reached.

## Self-improvement

Wiggum records every cycle, but self-analysis is eligible only after thirty
completed cycles and then every fifty new cycles, with at least three supporting
examples for a normal proposal. Serious regressions and repeated rollback or
blocker signals may trigger earlier analysis. The analysis command proposes
changes against recent and full-history windows. Implementation experiments are
kept on isolated `wiggum/self-improve/<id>` branches; promotion into this trusted
runtime remains a human action.

## Test

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

See `docs/decisions.md` for choices made where the Notion design leaves details
open.
