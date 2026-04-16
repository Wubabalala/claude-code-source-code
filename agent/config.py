"""Agent configuration — Phase 4 TOML + env + defaults.

Priority: built-in defaults < TOML file < env vars < CLI flags (CLI Phase 4
not implemented, reserved for later).

Safety floor constants (HARD_DENY_*, GREP_EXCLUDE_GLOBS, nested-interpreter
list) remain in agent/permissions.py and agent/tools.py — they are NOT
configurable via TOML to prevent users from accidentally weakening the
security boundary.

Usage:
    from agent.config import load_config
    cfg = load_config()                   # ~/.agent/config.toml or defaults
    cfg = load_config(Path("custom.toml"))
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionConfig:
    base_dir: str = "~/.agent/sessions"
    rotation_keep_days: int = 30
    resume_enabled: bool = True


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    audit_file: str = "~/.agent/logs/audit.jsonl"
    max_bytes: int = 10_000_000
    backup_count: int = 5


@dataclass(frozen=True)
class CompactConfig:
    ctx_window_tokens: int = 200_000
    micro_threshold: float = 0.70
    auto_threshold: float = 0.85
    max_consecutive_failures: int = 3
    keep_recent_tool_results: int = 5
    keep_recent_messages_in_auto: int = 4
    auto_compact_max_output: int = 4_096


@dataclass(frozen=True)
class ReplConfig:
    max_turns_per_query: int = 25
    primary_model: str = "claude-opus-4-6"
    fallback_model: str = "claude-sonnet-4-6"


@dataclass(frozen=True)
class PermissionsConfig:
    ask_prompt_prefix: str = "[PERMISSION]"
    non_tty_default: str = "DENY"


@dataclass(frozen=True)
class AgentConfig:
    session: SessionConfig = field(default_factory=SessionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    compact: CompactConfig = field(default_factory=CompactConfig)
    repl: ReplConfig = field(default_factory=ReplConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)


# Sections whose name is reserved but content is safety-floor (ignored with warn)
_SAFETY_FLOOR_KEYS: set[str] = {"hard_deny", "hard_deny_dir_components",
                                 "hard_deny_filenames", "hard_deny_patterns",
                                 "grep_exclude_globs", "nested_interpreters"}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _warn(msg: str) -> None:
    print(f"[config] warn: {msg}", file=sys.stderr)


def _coerce(value: Any, expected: type) -> Optional[Any]:
    """Return value coerced to expected type, or None on mismatch."""
    if expected is bool:
        return value if isinstance(value, bool) else None
    if expected is int:
        if isinstance(value, bool):
            return None  # bool is subclass of int; reject explicit bool here
        return value if isinstance(value, int) else None
    if expected is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None
    if expected is str:
        return value if isinstance(value, str) else None
    return None


def _build_section(section_cls: type, raw: dict, section_name: str) -> Any:
    """Build a frozen dataclass section from a raw TOML dict.

    Unknown keys → warn + ignore. Type-mismatched values → warn + use default.
    """
    if not is_dataclass(section_cls):
        raise TypeError(f"Not a dataclass: {section_cls}")

    known_field_types = {f.name: f.type for f in fields(section_cls)}
    kwargs: dict[str, Any] = {}

    for key, value in raw.items():
        # safety-floor guard: permissions section rejects hard_deny_*
        if section_name == "permissions" and key in _SAFETY_FLOOR_KEYS:
            _warn(
                f"[permissions].{key} is a safety-floor constant and "
                "cannot be set via config; ignoring"
            )
            continue

        if key not in known_field_types:
            _warn(f"unknown key [{section_name}].{key}; ignoring")
            continue

        expected = known_field_types[key]
        # field.type may be a string when using `from __future__ import annotations`;
        # map string names to real types for our known fields.
        type_map = {"bool": bool, "int": int, "float": float, "str": str}
        if isinstance(expected, str):
            expected = type_map.get(expected, str)

        coerced = _coerce(value, expected)
        if coerced is None:
            _warn(
                f"[{section_name}].{key} has wrong type "
                f"(expected {expected.__name__}, got {type(value).__name__}); "
                "using default"
            )
            continue

        kwargs[key] = coerced

    return section_cls(**kwargs)


def _apply_env_overrides(cfg: AgentConfig) -> AgentConfig:
    """Env vars override config file values (but not CLI flags, which are
    out of scope for Phase 4)."""
    from dataclasses import replace

    repl = cfg.repl
    repl_kwargs: dict[str, Any] = {}
    if (v := os.environ.get("AGENT_PRIMARY_MODEL")):
        repl_kwargs["primary_model"] = v
    if (v := os.environ.get("AGENT_FALLBACK_MODEL")):
        repl_kwargs["fallback_model"] = v
    if repl_kwargs:
        repl = replace(repl, **repl_kwargs)

    if repl is cfg.repl:
        return cfg
    return replace(cfg, repl=repl)


def load_config(path: Optional[Path] = None) -> AgentConfig:
    """Load AgentConfig from disk + env, falling back to defaults on any
    problem (with stderr warnings)."""
    if path is None:
        env_path = os.environ.get("AGENT_CONFIG_PATH")
        if env_path:
            path = Path(env_path).expanduser()
        else:
            path = Path("~/.agent/config.toml").expanduser()

    if not path.exists():
        return _apply_env_overrides(AgentConfig())

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        _warn(f"failed to parse {path}: {e}; using defaults")
        return _apply_env_overrides(AgentConfig())

    section_map = {
        "session": SessionConfig,
        "logging": LoggingConfig,
        "compact": CompactConfig,
        "repl": ReplConfig,
        "permissions": PermissionsConfig,
    }
    section_kwargs: dict[str, Any] = {}

    for section_name, section_cls in section_map.items():
        section_raw = raw.pop(section_name, None)
        if section_raw is None:
            continue
        if not isinstance(section_raw, dict):
            _warn(f"[{section_name}] is not a table; ignoring")
            continue
        section_kwargs[section_name] = _build_section(
            section_cls, section_raw, section_name
        )

    for unknown in raw:
        _warn(f"unknown section [{unknown}]; ignoring")

    cfg = AgentConfig(**section_kwargs)
    return _apply_env_overrides(cfg)
