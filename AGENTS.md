# Wiggum project constitution

Wiggum is a reusable, auditable software iteration harness. Product-specific
truth stays in each target repository; Wiggum owns orchestration, prompts,
policies, state, and guardrails.

## Invariants

- Never run against the target's protected base branch.
- Start each cycle from a clean integration branch and an identifiable commit.
- Work in an isolated Git worktree; only fast-forward reviewed, validated commits
  onto the integration branch.
- Never push, force-push, open a pull request, or rewrite a shared branch
  automatically.
- Red-risk work is deferred for a human decision. Preserve yellow-risk work in
  an isolated experiment until evidence supports integration.
- A no-change result is valid only with an evidence record and rationale.
- Preserve the original Codex JSONL event stream after redacting common secret
  formats. Store evidence outside target repositories with private permissions.
- Never weaken tests to make a change pass, invent domain facts, or hide failures.
- Wiggum may experiment on itself only on a separate self-improvement branch;
  it must never promote or activate its own code automatically.
- Use atomic semantic commits. Never commit credentials or raw secret files.

## Default operating limits

- One cycle per invocation unless the operator chooses more or requests an
  indefinite run.
- One active mode at a time; stop and pivot requests take effect between cycles.
- Stop after five consecutive cycles with no candidate meeting the value bar.
- Stop for integration review after fifteen successful commits by default.
- Codex defaults to `gpt-6-luna` with `max` reasoning effort.

When editing this repository, keep the runtime dependency-free, add regression
coverage for behavior changes, and record decisions in `docs/decisions.md`.
