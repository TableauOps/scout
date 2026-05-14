# Contributing to Scout

Thanks for considering a contribution. Scout is small, opinionated, and built around a deliberate scope: **recon for Tableau ops, never execution.** Contributions that fit that scope are welcome.

## What's in scope

- New investigation tools (read-only Tableau API queries) for the agent
- Better triage heuristics for the system prompt
- Better plain-English fix steps in `remediation.py`
- Performance improvements (cache, batching, rate-limit handling)
- Bug fixes anywhere
- Documentation

## What's out of scope

- **Anything that mutates a Tableau site** — Scout is read-only on Tableau by design. Execution lives in [TableauOps Autopilot](https://tableauops.com/scout), the paid action arm. PRs that add `update_*`, `refresh()`, `embed_credentials`, etc. will be declined regardless of how cleanly they're written.
- **REST endpoints, HTTP method names, payload bodies, or curl commands in any user-facing output** (terminal, audit log). The system prompt forbids it; reviewers will reject leaks.
- **Telemetry / phone-home.** Scout does not call out to any service it didn't authenticate to (Tableau, Anthropic). No analytics, no usage reporting.
- Raising the autonomy ceiling. `MAX_AUTONOMY = 1` is a load-bearing product decision — Scout OSS diagnoses, it does not deliver or execute. Delivery (Slack/PagerDuty/Teams) and execution live in TableauOps Autopilot. Forks are free to change it; upstream won't.

## Before you open a PR

1. **Open an issue first** for anything bigger than a typo. Saves both of us time.
2. **Run the agent end-to-end** against a real Tableau site to make sure you didn't break the diagnostic flow:
   ```bash
   python pull_jobs.py --reset
   python agent.py
   ```
3. **Check for REST leaks.** Inspect terminal output + audit_log JSON for any `PUT/POST/PATCH/DELETE`, `/api/3.x/`, curl syntax, or wire-format. If the system prompt or `remediation.py` started emitting them, that's a regression.
4. **No new dependencies** without a strong reason. Scout has a small footprint; let's keep it.

## Code style

- Match the surrounding code. No formatter is enforced; readability is.
- Comments explain _why_, not _what_. The code already says _what_.
- Tests are nice but not required for small changes; behavior on the demo scenarios is the integration test.

## Commit messages

One-line summary, present tense, lowercase first word, no period. Body optional but welcome for non-obvious changes.

## License

By contributing, you agree your contribution is licensed under [Apache 2.0](LICENSE) — same as the rest of the project.

## Questions

Open an issue, or email eric@tableauops.com.
