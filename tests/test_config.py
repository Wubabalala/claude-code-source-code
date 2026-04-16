"""Tests for agent.config — Phase 4 TOML loader + env override + defaults."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.config import (
    AgentConfig,
    CompactConfig,
    PermissionsConfig,
    load_config,
)


def test_defaults_when_no_toml(tmp_path, monkeypatch, capsys):
    """No file → built-in defaults, no warning."""
    monkeypatch.delenv("AGENT_PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("AGENT_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("AGENT_CONFIG_PATH", raising=False)
    cfg = load_config(tmp_path / "nonexistent.toml")
    assert cfg == AgentConfig()
    err = capsys.readouterr().err
    assert "warn" not in err.lower()


def test_toml_overrides_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_PRIMARY_MODEL", raising=False)
    monkeypatch.delenv("AGENT_FALLBACK_MODEL", raising=False)
    p = tmp_path / "config.toml"
    p.write_text("[compact]\nctx_window_tokens = 5000\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.compact.ctx_window_tokens == 5000
    # Other fields keep defaults
    assert cfg.compact.micro_threshold == 0.70
    assert cfg.repl.primary_model == "claude-opus-4-6"


def test_env_overrides_toml(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text("[repl]\nprimary_model = 'from-toml'\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_PRIMARY_MODEL", "from-env")
    cfg = load_config(p)
    assert cfg.repl.primary_model == "from-env"


def test_invalid_value_falls_back_to_default_with_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENT_PRIMARY_MODEL", raising=False)
    p = tmp_path / "config.toml"
    p.write_text("[compact]\nmicro_threshold = 'oops'\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.compact.micro_threshold == 0.70  # default
    err = capsys.readouterr().err
    assert "micro_threshold" in err
    assert "warn" in err.lower()


def test_malformed_toml_uses_defaults(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENT_PRIMARY_MODEL", raising=False)
    p = tmp_path / "config.toml"
    p.write_text("[this is not valid toml\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg == AgentConfig()
    err = capsys.readouterr().err
    assert "failed to parse" in err


def test_unknown_toml_key_ignored_with_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENT_PRIMARY_MODEL", raising=False)
    p = tmp_path / "config.toml"
    p.write_text("[compact]\nmystery_knob = 42\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg == AgentConfig()
    err = capsys.readouterr().err
    assert "mystery_knob" in err


def test_unknown_section_ignored_with_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENT_PRIMARY_MODEL", raising=False)
    p = tmp_path / "config.toml"
    p.write_text("[unknown_section]\nk = 'v'\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg == AgentConfig()
    err = capsys.readouterr().err
    assert "unknown_section" in err


def test_safety_floor_constants_not_in_config():
    """Contract C: safety-floor constants must not be fields of AgentConfig."""
    from dataclasses import fields
    perm_field_names = {f.name for f in fields(PermissionsConfig)}
    for banned in ("hard_deny", "hard_deny_dir_components",
                    "hard_deny_filenames", "hard_deny_patterns",
                    "grep_exclude_globs", "nested_interpreters"):
        assert banned not in perm_field_names


def test_agent_config_path_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_PRIMARY_MODEL", raising=False)
    p = tmp_path / "custom.toml"
    p.write_text("[compact]\nctx_window_tokens = 42\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CONFIG_PATH", str(p))
    cfg = load_config()  # no explicit path — should pick up env
    assert cfg.compact.ctx_window_tokens == 42


def test_hard_deny_not_configurable_via_toml(tmp_path, monkeypatch, capsys):
    """Contract C: attempting to set hard_deny in [permissions] must be
    rejected with a warning."""
    monkeypatch.delenv("AGENT_PRIMARY_MODEL", raising=False)
    p = tmp_path / "config.toml"
    p.write_text(
        "[permissions]\nhard_deny = ['foo']\n"
        "ask_prompt_prefix = 'CUSTOM'\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.permissions.ask_prompt_prefix == "CUSTOM"
    err = capsys.readouterr().err
    assert "hard_deny" in err
    assert "safety-floor" in err


def test_bool_field_parses(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_PRIMARY_MODEL", raising=False)
    p = tmp_path / "config.toml"
    p.write_text("[session]\nresume_enabled = false\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.session.resume_enabled is False


def test_int_rejects_bool(tmp_path, monkeypatch, capsys):
    """bool is a subclass of int in Python — our coerce rejects explicit bools
    where int is expected to catch obvious TOML mis-types."""
    monkeypatch.delenv("AGENT_PRIMARY_MODEL", raising=False)
    p = tmp_path / "config.toml"
    p.write_text("[compact]\nmax_consecutive_failures = true\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.compact.max_consecutive_failures == 3  # default
    err = capsys.readouterr().err
    assert "max_consecutive_failures" in err


def test_float_accepts_int(tmp_path, monkeypatch):
    """TOML 1 (int) should be accepted where float is expected."""
    monkeypatch.delenv("AGENT_PRIMARY_MODEL", raising=False)
    p = tmp_path / "config.toml"
    p.write_text("[compact]\nmicro_threshold = 1\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.compact.micro_threshold == 1.0


def test_env_only_overrides_repl_models(tmp_path, monkeypatch):
    """AGENT_PRIMARY_MODEL / AGENT_FALLBACK_MODEL override; other env vars
    used by the client (ANTHROPIC_API_KEY etc.) don't appear in config."""
    monkeypatch.delenv("AGENT_PRIMARY_MODEL", raising=False)
    monkeypatch.setenv("AGENT_FALLBACK_MODEL", "zz-fallback")
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.repl.fallback_model == "zz-fallback"
    # primary keeps default
    assert cfg.repl.primary_model == "claude-opus-4-6"


def test_empty_toml_is_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_PRIMARY_MODEL", raising=False)
    p = tmp_path / "config.toml"
    p.write_text("", encoding="utf-8")
    cfg = load_config(p)
    assert cfg == AgentConfig()
