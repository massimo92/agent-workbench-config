from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - local macOS python can lack tomllib.
    tomllib = None  # type: ignore[assignment]

try:
    import questionary
    from rich.console import Console
    from rich.table import Table
except ModuleNotFoundError:  # pragma: no cover - doctor reports this after install.
    questionary = None
    Console = None
    Table = None


def path_from_env(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    return Path(value).expanduser()


def first_existing_or_default(paths: list[Path]) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def user_config_home() -> Path:
    override = path_from_env("XDG_CONFIG_HOME")
    if override is not None:
        return override
    if sys.platform == "win32":
        appdata = path_from_env("APPDATA")
        if appdata is not None:
            return appdata
        return Path.home() / "AppData" / "Roaming"
    return Path.home() / ".config"


def app_support_dir(app_name: str) -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    if sys.platform == "win32":
        return user_config_home() / app_name
    return first_existing_or_default(
        [user_config_home() / app_name, user_config_home() / app_name.lower()]
    )


REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "manifest.json"
MCP_PATH = REPO_ROOT / "mcp" / "shared.json"
AGYNC_CONFIG_HOME = path_from_env("AGYNC_CONFIG_HOME") or (
    user_config_home() / "agent-workbench"
)
ENV_PATH = AGYNC_CONFIG_HOME / "env"
BACKUP_ROOT = AGYNC_CONFIG_HOME / "backups"
CODEX_HOME = path_from_env("CODEX_HOME") or Path.home() / ".codex"
CODEX_CONFIG = CODEX_HOME / "config.toml"
CLAUDE_APP_SUPPORT = path_from_env("AGYNC_CLAUDE_HOME") or app_support_dir("Claude")
CLAUDE_CONFIG = CLAUDE_APP_SUPPORT / "claude_desktop_config.json"
CLAUDE_DESKTOP_SKILLS_PLUGIN_ROOT = (
    CLAUDE_APP_SUPPORT / "local-agent-mode-sessions" / "skills-plugin"
)
CODEX_SKILLS = path_from_env("AGYNC_CODEX_SKILLS") or first_existing_or_default(
    [Path.home() / ".agents" / "skills", CODEX_HOME / "skills"]
)
OPENCODE_HOME = path_from_env("AGYNC_OPENCODE_HOME") or user_config_home() / "opencode"
OPENCODE_SKILLS = OPENCODE_HOME / "skills"
OPENCODE_CONFIGS = [
    OPENCODE_HOME / "opencode.json",
    OPENCODE_HOME / "config.json",
]

SAFE_ENV_KEYS = {"REST_BASE_URL", "HEADER_Accept"}
SECRET_KEY_PARTS = ("TOKEN", "KEY", "SECRET", "AUTH", "BEARER", "PASSWORD")


@dataclass(frozen=True)
class Skill:
    name: str
    path: Path


@dataclass(frozen=True)
class AppTarget:
    name: str
    display_name: str
    kind: Literal["codex", "claude", "opencode", "generic"]
    skills_path: Path | None
    mcp_config_path: Path | None = None


@dataclass(frozen=True)
class AppState:
    target: AppTarget
    skills: dict[str, Path]
    mcp_servers: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class Diff:
    app: AppState
    skills_only_in_repo: set[str]
    skills_only_in_app: set[str]
    mcp_only_in_repo: set[str]
    mcp_only_in_app: set[str]


@dataclass(frozen=True)
class Operation:
    action: Literal["import", "export"]
    item_type: Literal["skill", "mcp"]
    name: str
    target: AppTarget


def console_print(message: str = "") -> None:
    if Console is None:
        print(message)
    else:
        Console().print(message)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def load_manifest() -> dict[str, Any]:
    return load_json(MANIFEST_PATH)


def save_manifest(manifest: dict[str, Any]) -> None:
    manifest["skills"] = sorted(manifest.get("skills", []), key=lambda item: item["name"])
    manifest["mcp_servers"] = sorted(set(manifest.get("mcp_servers", [])))
    write_json(MANIFEST_PATH, manifest)


def load_mcp_registry() -> dict[str, Any]:
    if not MCP_PATH.exists():
        return {}
    return load_json(MCP_PATH)


def save_mcp_registry(registry: dict[str, Any]) -> None:
    MCP_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json(MCP_PATH, dict(sorted(registry.items())))


def enabled_skills() -> list[Skill]:
    skills = []
    for item in load_manifest()["skills"]:
        skills.append(Skill(name=item["name"], path=REPO_ROOT / item["path"]))
    return skills


def repo_skill_map() -> dict[str, Path]:
    return {skill.name: skill.path for skill in enabled_skills()}


def enabled_mcp_servers() -> dict[str, dict[str, Any]]:
    registry = load_mcp_registry()
    return {name: registry[name] for name in load_manifest()["mcp_servers"] if name in registry}


def load_env() -> dict[str, str]:
    values = dict(os.environ)
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def bootstrap_env_from_existing() -> bool:
    return False


def resolve_env_value(value: str, env: dict[str, str], *, placeholder: bool) -> str:
    match = re.fullmatch(r"\$\{([A-Z0-9_]+)\}", value)
    if not match:
        return value
    key = match.group(1)
    if placeholder:
        return value
    if key not in env or not env[key]:
        raise RuntimeError(f"Missing required env var: {key}")
    return env[key]


def server_for_host(
    name: str,
    host: str,
    env: dict[str, str],
    *,
    placeholder: bool,
    registry: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    base = (registry or enabled_mcp_servers())[name]
    override = base.get("hosts", {}).get(host, {})
    result = {
        "command": override.get("command", base["command"]),
        "args": override.get("args", base.get("args", [])),
        "env": {},
    }
    for key, value in base.get("env", {}).items():
        result["env"][key] = resolve_env_value(value, env, placeholder=placeholder)
    return result


def toml_string(value: str) -> str:
    return json.dumps(value)


def render_toml_array(values: list[str]) -> str:
    if not values:
        return "[]"
    body = "\n".join(f"    {toml_string(value)}," for value in values)
    return "[\n" + body + "\n]"


def render_codex_mcp(
    env: dict[str, str],
    *,
    placeholder: bool = False,
    names: set[str] | None = None,
) -> str:
    blocks: list[str] = []
    registry = enabled_mcp_servers()
    for name in registry:
        if names is not None and name not in names:
            continue
        server = server_for_host(name, "codex_app", env, placeholder=placeholder, registry=registry)
        block = [
            f"[mcp_servers.{name}]",
            f"command = {toml_string(server['command'])}",
            f"args = {render_toml_array(server['args'])}",
        ]
        if server["env"]:
            block.append("")
            block.append(f"[mcp_servers.{name}.env]")
            for key, value in server["env"].items():
                block.append(f"{key} = {toml_string(value)}")
        tools = registry[name].get("codex_tools", {})
        for tool_name, config in tools.items():
            block.append("")
            block.append(f"[mcp_servers.{name}.tools.{tool_name}]")
            for key, value in config.items():
                block.append(f"{key} = {toml_string(value)}")
        blocks.append("\n".join(block))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_claude_mcp(
    env: dict[str, str],
    *,
    placeholder: bool = False,
    names: set[str] | None = None,
) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    registry = enabled_mcp_servers()
    for name in registry:
        if names is not None and name not in names:
            continue
        server = server_for_host(name, "claude_desktop", env, placeholder=placeholder, registry=registry)
        entry = {"command": server["command"], "args": server["args"]}
        if server["env"]:
            entry["env"] = server["env"]
        servers[name] = entry
    return {"mcpServers": servers}


def remove_codex_mcp_sections(text: str, names: set[str]) -> str:
    result: list[str] = []
    skip = False
    target_prefixes = tuple(f"[mcp_servers.{name}" for name in names)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(target_prefixes):
            skip = True
            continue
        if skip and stripped.startswith("["):
            skip = False
        if not skip:
            result.append(line)
    return "\n".join(result).rstrip() + "\n"


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def backup_file(path: Path, backup_dir: Path) -> Path | None:
    if not path.exists():
        return None
    target = backup_dir / "files" / str(path).lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target


def backup_and_replace_path(path: Path, source: Path, backup_dir: Path, dry_run: bool) -> str:
    if path.is_symlink() and path.resolve() == source.resolve():
        return f"ok: {path} already points to {source}"
    if path.exists() or path.is_symlink():
        backup = backup_dir / "skills" / path.parent.name / path.name
        if dry_run:
            return f"would replace {path} with symlink to {source}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(backup))
    elif dry_run:
        return f"would link {path} -> {source}"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(source, target_is_directory=True)
    return f"linked: {path} -> {source}"


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    data = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def validate_skill(skill: Skill) -> list[str]:
    errors = []
    skill_file = skill.path / "SKILL.md"
    if not skill_file.exists():
        return [f"{skill.name}: missing SKILL.md"]
    meta = parse_frontmatter(skill_file.read_text())
    name = meta.get("name")
    description = meta.get("description")
    if name != skill.name:
        errors.append(f"{skill.name}: frontmatter name is {name!r}")
    if not description:
        errors.append(f"{skill.name}: missing description")
    if name and not re.fullmatch(r"[a-z0-9-]{1,64}", name):
        errors.append(f"{skill.name}: invalid name format")
    return errors


def find_broken_symlinks(paths: list[Path]) -> list[Path]:
    broken = []
    for root in paths:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_symlink() and not path.exists():
                broken.append(path)
    return broken


def command_exists(command: str) -> bool:
    return Path(command).exists() if "/" in command else shutil.which(command) is not None


def check_no_repo_secrets() -> list[str]:
    patterns = [
        re.compile(r"AUTH_BEARER\s*=\s*[A-Za-z0-9_-]{20,}"),
        re.compile(r"[A-Z0-9_]*(TOKEN|SECRET|PASSWORD|API_KEY)\s*=\s*.+"),
    ]
    errors = []
    for path in REPO_ROOT.rglob("*"):
        if path.is_dir() or ".git" in path.parts:
            continue
        if "__pycache__" in path.parts or path.suffix in {".py", ".pyc"} or path.name == ".env.example":
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for pattern in patterns:
            if pattern.search(text):
                errors.append(f"possible secret in {path.relative_to(REPO_ROOT)}")
    return errors


def scan_skill_dir(root: Path | None) -> dict[str, Path]:
    if root is None or not root.exists():
        return {}
    skills: dict[str, Path] = {}
    for child in sorted(root.iterdir()):
        if child.name.startswith("."):
            continue
        skill_file = child / "SKILL.md"
        if skill_file.exists():
            skills[child.name] = child
    return skills


def codex_mcp_servers(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    if tomllib is None:
        return parse_codex_mcp_simple(path.read_text())
    data = tomllib.loads(path.read_text())
    servers = data.get("mcp_servers", {})
    result: dict[str, dict[str, Any]] = {}
    for name, value in servers.items():
        if not isinstance(value, dict):
            continue
        if "command" not in value and "url" not in value:
            continue
        result[name] = normalize_app_mcp(value)
    return result


def parse_codex_mcp_simple(text: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    current: str | None = None
    current_env: str | None = None
    pending_array_key: str | None = None
    pending_array: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        section = re.fullmatch(r"\[mcp_servers\.([A-Za-z0-9_-]+)(?:\.env)?(?:\..*)?\]", line)
        if section:
            if pending_array_key and current:
                result[current][pending_array_key] = pending_array
                pending_array_key = None
                pending_array = []
            current = section.group(1)
            result.setdefault(current, {"command": "", "args": [], "env": {}})
            current_env = current if line.endswith(".env]") else None
            continue
        if current is None:
            continue
        if pending_array_key:
            if line == "]":
                result[current][pending_array_key] = pending_array
                pending_array_key = None
                pending_array = []
            else:
                pending_array.extend(re.findall(r'"([^"]*)"', line))
            continue
        if "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        if value == "[":
            pending_array_key = key
            pending_array = []
            continue
        string_match = re.fullmatch(r'"([^"]*)"', value)
        parsed_value: Any = string_match.group(1) if string_match else value
        if current_env:
            result[current]["env"][key] = parsed_value
        elif key in {"command", "url"}:
            result[current][key] = parsed_value
    return {
        name: normalize_app_mcp(server)
        for name, server in result.items()
        if server.get("command") or server.get("url")
    }


def claude_mcp_servers(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = load_json(path)
    except json.JSONDecodeError:
        return {}
    return {
        name: normalize_app_mcp(value)
        for name, value in data.get("mcpServers", {}).items()
        if isinstance(value, dict)
    }


def opencode_config_path() -> Path | None:
    return next((path for path in OPENCODE_CONFIGS if path.exists()), None)


def opencode_mcp_servers(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    try:
        data = load_json(path)
    except json.JSONDecodeError:
        return {}
    candidates = data.get("mcp") or data.get("mcpServers") or {}
    return {
        name: normalize_app_mcp(value)
        for name, value in candidates.items()
        if isinstance(value, dict)
    }


def opencode_supports_mcp(path: Path | None) -> bool:
    if path is None or not path.exists():
        return False
    try:
        data = load_json(path)
    except json.JSONDecodeError:
        return False
    return "mcp" in data or "mcpServers" in data


def target_supports_mcp(target: AppTarget) -> bool:
    if target.kind in {"codex", "claude"}:
        return target.mcp_config_path is not None
    if target.kind == "opencode":
        return opencode_supports_mcp(target.mcp_config_path)
    return False


def normalize_app_mcp(value: dict[str, Any]) -> dict[str, Any]:
    server = {
        "command": value.get("command", ""),
        "args": value.get("args", []),
        "env": value.get("env", {}),
    }
    if value.get("url"):
        server["url"] = value["url"]
    if not isinstance(server["args"], list):
        server["args"] = []
    if not isinstance(server["env"], dict):
        server["env"] = {}
    return server


def claude_desktop_skills_path() -> Path | None:
    if not CLAUDE_DESKTOP_SKILLS_PLUGIN_ROOT.exists():
        return None
    candidates = [
        path
        for path in CLAUDE_DESKTOP_SKILLS_PLUGIN_ROOT.glob("*/*/skills")
        if path.is_dir()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def detect_targets() -> list[AppTarget]:
    targets = [
        AppTarget("codex", "Codex app", "codex", CODEX_SKILLS, CODEX_CONFIG),
        AppTarget(
            "claude",
            "Claude Desktop/Cowork",
            "claude",
            claude_desktop_skills_path(),
            CLAUDE_CONFIG,
        ),
    ]
    if OPENCODE_SKILLS.exists() or opencode_config_path() is not None:
        targets.append(AppTarget("opencode", "OpenCode", "opencode", OPENCODE_SKILLS, opencode_config_path()))
    return targets


def app_state(target: AppTarget) -> AppState:
    if target.kind == "codex" and target.mcp_config_path:
        mcp = codex_mcp_servers(target.mcp_config_path)
    elif target.kind == "claude" and target.mcp_config_path:
        mcp = claude_mcp_servers(target.mcp_config_path)
    elif target.kind == "opencode":
        mcp = opencode_mcp_servers(target.mcp_config_path)
    else:
        mcp = {}
    return AppState(target=target, skills=scan_skill_dir(target.skills_path), mcp_servers=mcp)


def all_app_states() -> list[AppState]:
    return [app_state(target) for target in detect_targets()]


def diff_for_app(state: AppState) -> Diff:
    repo_skills = set(repo_skill_map())
    app_skills = set(state.skills)
    repo_mcp = set(enabled_mcp_servers()) if target_supports_mcp(state.target) else set()
    app_mcp = set(state.mcp_servers)
    return Diff(
        app=state,
        skills_only_in_repo=repo_skills - app_skills,
        skills_only_in_app=app_skills - repo_skills,
        mcp_only_in_repo=repo_mcp - app_mcp,
        mcp_only_in_app=app_mcp - repo_mcp,
    )


def print_diff(diff: Diff) -> None:
    print(f"\n{diff.app.target.display_name}")
    rows = [
        ("skills repo -> app", diff.skills_only_in_repo),
        ("skills app -> repo", diff.skills_only_in_app),
        ("mcp repo -> app", diff.mcp_only_in_repo),
        ("mcp app -> repo", diff.mcp_only_in_app),
    ]
    if not any(values for _, values in rows):
        print("- no diff")
        return
    for label, values in rows:
        if values:
            print(f"- {label}: {', '.join(sorted(values))}")


def print_inventory() -> None:
    print(f"repo: {REPO_ROOT}")
    print(f"repo skills: {len(repo_skill_map())}")
    print(f"repo MCPs: {len(enabled_mcp_servers())}")
    for state in all_app_states():
        print_diff(diff_for_app(state))


def select_targets(names: list[str] | None) -> list[AppState]:
    states = all_app_states()
    if not names:
        return states
    requested = set(names)
    selected = [state for state in states if state.target.name in requested]
    missing = requested - {state.target.name for state in selected}
    if missing:
        raise RuntimeError(f"unknown app target(s): {', '.join(sorted(missing))}")
    return selected


def operations_for_import(states: list[AppState], selected: set[str] | None) -> list[Operation]:
    operations: list[Operation] = []
    repo_skills = set(repo_skill_map())
    repo_mcp = set(enabled_mcp_servers())
    imported: set[tuple[str, str]] = set()
    for state in states:
        for name in sorted(set(state.skills) - repo_skills):
            if selected is None or f"skill:{name}" in selected or name in selected:
                if ("skill", name) in imported:
                    continue
                imported.add(("skill", name))
                operations.append(Operation("import", "skill", name, state.target))
        for name in sorted(set(state.mcp_servers) - repo_mcp):
            if selected is None or f"mcp:{name}" in selected or name in selected:
                if ("mcp", name) in imported:
                    continue
                imported.add(("mcp", name))
                operations.append(Operation("import", "mcp", name, state.target))
    return operations


def operations_for_export(states: list[AppState], selected: set[str] | None) -> list[Operation]:
    operations: list[Operation] = []
    repo_skills = set(repo_skill_map())
    repo_mcp = set(enabled_mcp_servers())
    for state in states:
        for name in sorted(repo_skills - set(state.skills)):
            if selected is None or f"skill:{name}" in selected or name in selected:
                operations.append(Operation("export", "skill", name, state.target))
        if not target_supports_mcp(state.target):
            continue
        for name in sorted(repo_mcp - set(state.mcp_servers)):
            if selected is None or f"mcp:{name}" in selected or name in selected:
                operations.append(Operation("export", "mcp", name, state.target))
    return operations


def operations_for_sync(states: list[AppState], selected: set[str] | None) -> list[Operation]:
    return operations_for_import(states, selected) + operations_for_export(states, selected)


def print_operations(operations: list[Operation]) -> None:
    if not operations:
        print("no changes")
        return
    for op in operations:
        arrow = "app -> repo" if op.action == "import" else "repo -> app"
        print(f"- {op.target.display_name}: {op.item_type} {op.name} ({arrow})")


def backup_dir_for_run() -> Path:
    return BACKUP_ROOT / timestamp()


def import_skill(name: str, state: AppState, backup_dir: Path, dry_run: bool) -> str:
    source = state.skills[name]
    target = REPO_ROOT / "skills" / name
    if dry_run:
        return f"would import skill {name}: {source} -> {target}"
    if target.exists() or target.is_symlink():
        backup_and_replace_path(target, source, backup_dir, dry_run=False)
        if target.is_symlink():
            target.unlink()
    shutil.copytree(source.resolve(), target, symlinks=True, dirs_exist_ok=False)
    manifest = load_manifest()
    if name not in {item["name"] for item in manifest["skills"]}:
        manifest["skills"].append({"name": name, "path": f"skills/{name}"})
        save_manifest(manifest)
    return f"imported skill {name}"


def export_skill(name: str, state: AppState, backup_dir: Path, dry_run: bool) -> str:
    source = repo_skill_map()[name]
    if state.target.skills_path is None:
        return f"skipped skill {name}: {state.target.display_name} has no skills path"
    target = state.target.skills_path / name
    return backup_and_replace_path(target, source, backup_dir, dry_run)


def should_secret_placeholder(key: str, value: Any) -> bool:
    if key in SAFE_ENV_KEYS:
        return False
    if any(part in key.upper() for part in SECRET_KEY_PARTS):
        return True
    return isinstance(value, str) and len(value) >= 32 and not value.startswith("http")


def registry_entry_from_app(name: str, server: dict[str, Any]) -> dict[str, Any]:
    env: dict[str, Any] = {}
    required_env: list[str] = []
    for key, value in server.get("env", {}).items():
        if should_secret_placeholder(key, value):
            env_name = f"{name.upper().replace('-', '_')}_{key}"
            env[key] = f"${{{env_name}}}"
            required_env.append(env_name)
        else:
            env[key] = value
    entry: dict[str, Any] = {
        "description": f"Imported from app config: {name}.",
        "command": server.get("command", ""),
        "args": server.get("args", []),
        "env": env,
    }
    if server.get("url"):
        entry["url"] = server["url"]
    if required_env:
        entry["required_env"] = sorted(required_env)
    return entry


def import_mcp(name: str, state: AppState, dry_run: bool) -> str:
    server = state.mcp_servers[name]
    if dry_run:
        return f"would import MCP {name} from {state.target.display_name}"
    registry = load_mcp_registry()
    registry[name] = registry_entry_from_app(name, server)
    save_mcp_registry(registry)
    manifest = load_manifest()
    if name not in manifest["mcp_servers"]:
        manifest["mcp_servers"].append(name)
        save_manifest(manifest)
    return f"imported MCP {name}"


def export_mcp(name: str, state: AppState, backup_dir: Path, dry_run: bool) -> str:
    env = load_env()
    target = state.target
    if target.kind == "codex":
        return patch_codex_mcp({name}, env, backup_dir, dry_run)
    if target.kind == "claude":
        return patch_claude_mcp({name}, env, backup_dir, dry_run)
    return f"skipped MCP {name}: {target.display_name} MCP export is not supported yet"


def patch_codex_mcp(names: set[str], env: dict[str, str], backup_dir: Path, dry_run: bool) -> str:
    existing = CODEX_CONFIG.read_text() if CODEX_CONFIG.exists() else ""
    rendered = render_codex_mcp(env, names=names)
    updated = remove_codex_mcp_sections(existing, names).rstrip() + "\n\n" + rendered
    if dry_run:
        return f"would patch {CODEX_CONFIG}: {', '.join(sorted(names))}"
    backup_file(CODEX_CONFIG, backup_dir)
    CODEX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CODEX_CONFIG.write_text(updated)
    return f"patched {CODEX_CONFIG}: {', '.join(sorted(names))}"


def patch_claude_mcp(names: set[str], env: dict[str, str], backup_dir: Path, dry_run: bool) -> str:
    existing: dict[str, Any] = {}
    if CLAUDE_CONFIG.exists():
        existing = load_json(CLAUDE_CONFIG)
    existing.setdefault("mcpServers", {})
    existing["mcpServers"].update(render_claude_mcp(env, names=names)["mcpServers"])
    if dry_run:
        return f"would patch {CLAUDE_CONFIG}: {', '.join(sorted(names))}"
    backup_file(CLAUDE_CONFIG, backup_dir)
    CLAUDE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_CONFIG.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
    return f"patched {CLAUDE_CONFIG}: {', '.join(sorted(names))}"


def apply_operations(operations: list[Operation], dry_run: bool) -> list[str]:
    backup_dir = backup_dir_for_run()
    states = {state.target.name: state for state in all_app_states()}
    messages: list[str] = []
    for op in operations:
        state = states[op.target.name]
        if op.action == "import" and op.item_type == "skill":
            messages.append(import_skill(op.name, state, backup_dir, dry_run))
        elif op.action == "export" and op.item_type == "skill":
            messages.append(export_skill(op.name, state, backup_dir, dry_run))
        elif op.action == "import" and op.item_type == "mcp":
            messages.append(import_mcp(op.name, state, dry_run))
        elif op.action == "export" and op.item_type == "mcp":
            messages.append(export_mcp(op.name, state, backup_dir, dry_run))
    if not dry_run and operations:
        messages.append(f"backup: {backup_dir}")
    return messages


def install_skills(backup_dir: Path, dry_run: bool) -> list[str]:
    messages = []
    roots = [CODEX_SKILLS]
    claude_skills = claude_desktop_skills_path()
    if claude_skills is not None:
        roots.append(claude_skills)
    for skill in enabled_skills():
        for root in roots:
            messages.append(backup_and_replace_path(root / skill.name, skill.path, backup_dir, dry_run))
    return messages


def install_codex_mcp(env: dict[str, str], backup_dir: Path, dry_run: bool) -> str:
    return patch_codex_mcp(set(enabled_mcp_servers()), env, backup_dir, dry_run)


def install_claude_mcp(env: dict[str, str], backup_dir: Path, dry_run: bool) -> str:
    return patch_claude_mcp(set(enabled_mcp_servers()), env, backup_dir, dry_run)


def cmd_inventory(_: argparse.Namespace) -> int:
    print_inventory()
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    env = load_env()
    if args.target in ("codex", "all"):
        print("# Codex app TOML")
        print(render_codex_mcp(env, placeholder=True))
    if args.target in ("claude", "all"):
        print("# Claude Desktop JSON")
        print(json.dumps(render_claude_mcp(env, placeholder=True), indent=2))
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    errors: list[str] = []
    for skill in enabled_skills():
        errors.extend(validate_skill(skill))
    env = load_env()
    for name, server in enabled_mcp_servers().items():
        for key in server.get("required_env", []):
            if not env.get(key):
                errors.append(f"{name}: missing env var {key} in environment or {ENV_PATH}")
        for host in ("codex_app", "claude_desktop"):
            rendered = server_for_host(name, host, env, placeholder=True)
            if rendered.get("command") and not command_exists(rendered["command"]):
                errors.append(f"{name}/{host}: command not found: {rendered['command']}")
    if questionary is None or Console is None:
        errors.append("interactive deps missing: install package with uv tool install -e . --force")
    errors.extend(check_no_repo_secrets())
    if errors:
        print("doctor: failed")
        for error in errors:
            print(f"- {error}")
        return 1
    print("doctor: ok")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    if args.bootstrap_env:
        changed = bootstrap_env_from_existing()
        print(f"bootstrap env: {'created/updated' if changed else 'not needed'}")
    env = load_env()
    dry_run = not args.apply
    backup_dir = backup_dir_for_run()
    for message in install_skills(backup_dir, dry_run):
        print(message)
    print(install_codex_mcp(env, backup_dir, dry_run))
    print(install_claude_mcp(env, backup_dir, dry_run))
    if dry_run:
        print("dry run only; pass --apply to change files")
    else:
        print(f"backup: {backup_dir}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    states = select_targets(args.app)
    selected = None if args.all else set(args.item or [])
    operations = operations_for_import(states, selected)
    print_operations(operations)
    for message in apply_operations(operations, dry_run=not args.apply):
        print(message)
    if not args.apply:
        print("dry run only; pass --apply to change files")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    states = select_targets(args.app)
    selected = None if args.all else set(args.item or [])
    operations = operations_for_export(states, selected)
    print_operations(operations)
    for message in apply_operations(operations, dry_run=not args.apply):
        print(message)
    if not args.apply:
        print("dry run only; pass --apply to change files")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    states = select_targets(args.app)
    selected = None if args.all else set(args.item or [])
    operations = operations_for_sync(states, selected)
    print_operations(operations)
    for message in apply_operations(operations, dry_run=not args.apply):
        print(message)
    if not args.apply:
        print("dry run only; pass --apply to change files")
    return 0


def ask_or_exit(value: Any) -> Any:
    if value is None:
        raise SystemExit(1)
    return value


def require_interactive() -> None:
    if questionary is None or Console is None or Table is None:
        raise RuntimeError("interactive dependencies missing; run: uv tool install -e . --force")


def interactive_select_apps() -> list[AppState]:
    require_interactive()
    states = all_app_states()
    choices = [
        questionary.Choice(f"All ({len(states)} apps)", value="__all__", checked=True),
        *[
            questionary.Choice(state.target.display_name, value=state.target.name, checked=True)
            for state in states
        ],
    ]
    selected = ask_or_exit(questionary.checkbox("Apps:", choices=choices).ask())
    if "__all__" in selected:
        return states
    return [state for state in states if state.target.name in selected]


def interactive_select_operations(kind: Literal["import", "export", "sync"]) -> list[Operation]:
    require_interactive()
    states = interactive_select_apps()
    if kind == "import":
        operations = operations_for_import(states, None)
    elif kind == "export":
        operations = operations_for_export(states, None)
    else:
        operations = operations_for_sync(states, None)
    if not operations:
        console_print("[green]No changes.[/green]")
        return []
    choices = [
        questionary.Choice("All", value="__all__", checked=True),
        *[
            questionary.Choice(
                f"{op.target.display_name}: {op.item_type} {op.name} ({op.action})",
                value=f"{op.action}:{op.item_type}:{op.target.name}:{op.name}",
                checked=True,
            )
            for op in operations
        ],
    ]
    selected = ask_or_exit(questionary.checkbox("Changes:", choices=choices).ask())
    if "__all__" in selected:
        return operations
    selected_set = set(selected)
    return [
        op
        for op in operations
        if f"{op.action}:{op.item_type}:{op.target.name}:{op.name}" in selected_set
    ]


def interactive_apply(kind: Literal["import", "export", "sync"]) -> None:
    require_interactive()
    operations = interactive_select_operations(kind)
    if not operations:
        return
    console_print("\n[bold]Plan[/bold]")
    print_operations(operations)
    apply = ask_or_exit(questionary.confirm("Apply changes?", default=False).ask())
    for message in apply_operations(operations, dry_run=not apply):
        print(message)
    if not apply:
        print("dry run only")


def interactive_menu() -> int:
    require_interactive()
    while True:
        choice = ask_or_exit(
            questionary.select(
                "AGYNC - What do you want to do?",
                choices=[
                    "Inventory",
                    "Import from app to repo",
                    "Export repo to app",
                    "Sync repo and apps",
                    "Doctor",
                    "Exit",
                ],
            ).ask()
        )
        if choice == "Inventory":
            print_inventory()
        elif choice == "Import from app to repo":
            interactive_apply("import")
        elif choice == "Export repo to app":
            interactive_apply("export")
        elif choice == "Sync repo and apps":
            interactive_apply("sync")
        elif choice == "Doctor":
            cmd_doctor(argparse.Namespace())
        elif choice == "Exit":
            return 0
    return 0


def add_plan_command(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    func: Any,
) -> None:
    parser = sub.add_parser(name)
    parser.add_argument("--app", action="append", help="App target name, e.g. codex, claude, opencode.")
    parser.add_argument("--item", action="append", help="Item name or typed item like skill:example.")
    parser.add_argument("--all", action="store_true", help="Select all diff items.")
    parser.add_argument("--apply", action="store_true", help="Apply changes. Default is dry-run.")
    parser.set_defaults(func=func)


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    if not args_list:
        try:
            return interactive_menu()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    parser = argparse.ArgumentParser(prog="agync")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory").set_defaults(func=cmd_inventory)
    render = sub.add_parser("render")
    render.add_argument("--target", choices=["all", "codex", "claude"], default="all")
    render.set_defaults(func=cmd_render)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    install = sub.add_parser("install")
    install.add_argument("--apply", action="store_true")
    install.add_argument("--bootstrap-env", action="store_true")
    install.set_defaults(func=cmd_install)
    add_plan_command(sub, "import", cmd_import)
    add_plan_command(sub, "export", cmd_export)
    add_plan_command(sub, "sync", cmd_sync)
    args = parser.parse_args(args_list)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
