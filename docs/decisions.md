# Initial implementation decisions

The Notion Wiggum specification is the product source. This file records
implementation choices for its open details.

| Question | Initial decision |
| --- | --- |
| Runtime | Python 3.11+ and the standard library; use TOML instead of YAML to avoid a runtime dependency. |
| Model | `gpt-6-luna` with `max` reasoning effort by default; both are configurable. |
| Cycle length | One cycle per command by default. `--cycles N` and `--forever` are explicit. |
| Stop/pivot | Write a control request in private project state; honor it only at an atomic cycle boundary. |
| Target work | Require a clean configured integration branch; use a detached worktree for each cycle. |
| Risk | Red-risk requests are deferred. All changes are reviewed and validated before a fast-forward into the integration branch. |
| Publishing | No automatic push or PR creation. A batch threshold reports that human review is due. |
| State | Store state outside target repos under the XDG state directory with owner-only permissions. Preserve redacted Codex JSONL events and concise summaries. |
| Privacy | Redact common token and credential formats; document that the filter is best-effort. Never commit raw evidence. |
| Feature defaults | Product-minded autonomous selection. Council mode saves and prints five weighted candidates without implementing. |
| Browser evidence | Support a project-supplied screenshot command; no browser dependency is installed by Wiggum. |
| Self-improvement | Analyze after the configured evidence threshold; keep implementation experiments on an isolated branch and never promote automatically. |

The explicit design questions about external permissions, CI/review feedback,
issue trackers, feature score calibration, log retention, and concurrent runs
remain configurable or operator-managed until evidence supports stronger
defaults.
