# Mimit development

- Read the relevant Linear issues and current Notion product/architecture decisions
  before implementing a new workstream. Linear is the execution tracker; do not
  update ClickUp. Ask before changing a locked product or architecture decision.
- Include the primary Linear issue ID in feature branch names, preserving the
  `dev/` prefix (for example, `dev/mac-40-telegram-idempotency`).
- Reference relevant Linear IDs in commits. PR titles include the primary issue
  ID; PR descriptions link all relevant issues with their actual Linear URLs.
  Use closing keywords only for issues whose acceptance behavior is verified.
- Keep parent/milestone issues open until every required sub-issue and gate passes.
- Run `make check` and relevant real-PostgreSQL tests for persistence changes.
  Do not replace PostgreSQL with SQLite for concurrency/transaction guarantees.
- Keep credentials in ignored local environment files or deployment variables.
  Do not commit bot tokens or log Telegram URLs containing tokens.

## Project documentation for agents

Before implementation or a planning update, read `README.md`,
`docs/invariants.md`, and the relevant parts of `docs/verification.md`, then inspect
the affected code and existing changes. Read `docs/configuration.md`,
`docs/telegram.md`, and `docs/deployment.md` when the task touches those areas.
Distinguish recorded test results from checks run for the current change.

Notion is the source of truth for product intent, scope, architecture decisions,
and planned quality gates. If `.codex/project-docs.md` exists, read it for the
private hub and document links. Fetch the hub and the documents relevant to the
task through the connected Notion tools: Vision & Product Scope for product and
Telegram flows; Architecture & Infrastructure for technical boundaries; Build
Plan & Quality Gates for sequencing and acceptance; Decisions & Open Trade-offs
for locked decisions and unresolved choices. Read the current pages rather than
relying on remembered or copied content. Treat a planning target as a requirement,
not proof of implementation or permission to build it.

Current user instructions and recorded authorization take precedence. Notion
records product intent and decisions; repository contracts describe implemented
technical behavior; verification records establish evidence only under their
stated conditions. Linear remains the execution tracker. Reconcile stale
statements against code and tests, and surface material conflicts rather than
silently changing scope or guarantees. Ask before changing a locked decision.
Uncommitted work is not a completed milestone or a recorded product decision.
Preserve changes already in progress.

When documentation updates are within the requested scope, keep the hub and
affected planning summaries aligned with implementation, date the update, link
technical evidence, and separate implemented, in-progress, and deferred work.
Otherwise report relevant documentation drift. Do not duplicate detailed technical
contracts in Notion or claim unrun checks passed. Record scope, cost, and
reliability decisions in the decision log before expanding the project.

Keep `.codex/project-docs.md` ignored and never copy its private workspace links or
personal context into tracked files, commits, PRs, public docs, or logs. It is a
local reference and is not distributed with a clone. If it or Notion access is
unavailable, continue authorized work from repository evidence and report the
limitation; request missing context only when a consequential decision depends on
it. Do not invent or search for private URLs.
