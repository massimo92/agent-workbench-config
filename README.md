# Agent Workbench Config

`agync` is a local CLI for comparing and syncing Agent Skills and MCP server
definitions across local agent apps.

The repository ships the tool only. Skills and MCP registries are intentionally
ignored by git, so users can keep local/private agent configuration without
publishing it.

## Pain Points

Agent tooling is moving fast, but local configuration still tends to fragment:

- Skills are copied by hand between tools and quickly drift.
- MCP server definitions use different config formats per app.
- It is hard to see which app has a skill or MCP that another app is missing.

## Who Benefits

`agync` is useful for developers, DevOps engineers, AI power users, and teams
that run more than one local agent app, especially:

- Users who want to migrate agent apps, e.g. from Codex to Claude or vice versa.
- Engineers standardizing agent setup across laptops.

It is less useful when all skills and MCPs are specific to one app and should
never be shared.

## When To Use It

Use `agync` when you want to:

- Audit what each installed agent app has configured.
- Import reusable skills or MCPs from an app into a local shared repo.
- Export shared repo config into Codex app, Claude Desktop, or another supported
  target.
- Sync only the selected skills and MCPs that are meant to be portable.
- Keep app-specific tools out of the shared repo.

Run inventory before syncing:

```bash
agync inventory
```

Then run import, export, or sync in dry-run mode first. Add `--apply` only after
the plan looks right.

## Benefits

- One local source of truth for shared agent skills and MCP definitions.
- Explicit diffs by app, so missing items are visible before changes are made.
- Dry-run by default, with interactive confirmation in menu mode.
- Backups before replacing app config or skill folders.
- Secrets stay local through ignored files and environment placeholders.
- Public-safe repository layout: the tool can be published without publishing
  private skills, MCP registries, tokens, or machine-specific paths.
- Selective sync, so Codex-only and Claude-only capabilities can remain local.

## Install

```bash
uv tool install -e . --force
```

Then run:

```bash
agync
```

## Commands

```bash
agync inventory
agync doctor
agync import --app opencode --all
agync export --app claude --all --apply
agync sync --all
```

By default, import/export/sync are dry-run. Use `--apply` to write changes.
Interactive mode always shows a plan and asks for confirmation before applying.

## Platform Paths

`agync` detects the current OS and chooses app paths from the platform defaults:

- macOS: Claude Desktop under `~/Library/Application Support/Claude`; local tool
  data under `~/.config/agent-workbench`.
- Windows: Claude Desktop and local tool data under `%APPDATA%`.
- Linux: Claude Desktop under `$XDG_CONFIG_HOME/Claude` or
  `$XDG_CONFIG_HOME/claude`; local tool data under
  `$XDG_CONFIG_HOME/agent-workbench`.

Codex config uses `$CODEX_HOME/config.toml` when `CODEX_HOME` is set, otherwise
`~/.codex/config.toml`. Codex skills default to `~/.agents/skills`, falling back
to `$CODEX_HOME/skills`.

Override paths when an app stores data somewhere else:

```bash
AGYNC_CONFIG_HOME=/path/to/agync-data
AGYNC_CLAUDE_HOME=/path/to/Claude
AGYNC_CODEX_SKILLS=/path/to/codex/skills
AGYNC_OPENCODE_HOME=/path/to/opencode
```

## Local Data

Ignored local paths:

- `skills/*`
- `mcp/*.json`
- `.env`
- `.env.*`

Keep placeholders or local private values outside git. Use `.env.example` only as
a template.

## Safety

- Creates timestamped backups before replacing app files.
- Does not delete remote/app skills by default.
- Inventory shows diffs only: what exists in an app but not in the repo, and what
  exists in the repo but not in the app.
