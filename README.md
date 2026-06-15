# Agent Workbench Config

`agync` is a local CLI for comparing and syncing Agent Skills and MCP server
definitions across local agent apps.

The repository ships the tool only. Skills and MCP registries are intentionally
ignored by git, so users can keep local/private agent configuration without
publishing it.

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
