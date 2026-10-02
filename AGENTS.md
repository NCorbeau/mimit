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
