"""Team mailbox coordination — Phase 7 Layer 2.

File-based mailbox pattern for multi-agent collaboration:
  - Team directory: .agent/teams/{team_name}/
  - Members tracked in team.json with status state machine
  - Messages stored as individual JSON files in per-agent mailbox dirs
  - Two-phase consumption: peek (read without consuming) → ack (mark consumed)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


# Member status state machine: registered → running → completed | failed
STATUS_REGISTERED = "registered"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


@dataclass
class TeamMessage:
    id: str          # filename stem, e.g. "001"
    from_name: str
    to_name: str
    ts: str
    body: str
    consumed: bool
    path: Path


class TeamManager:
    """Manages team lifecycle and member registry."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)

    def _team_dir(self, team_name: str) -> Path:
        return self.base_dir / "teams" / team_name

    def _team_json(self, team_name: str) -> Path:
        return self._team_dir(team_name) / "team.json"

    def create_team(self, team_name: str, leader_name: str = "leader") -> Path:
        td = self._team_dir(team_name)
        td.mkdir(parents=True, exist_ok=True)
        (td / "mailboxes" / leader_name).mkdir(parents=True, exist_ok=True)
        team_data = {
            "name": team_name,
            "leader": leader_name,
            "members": [{"name": leader_name, "status": STATUS_REGISTERED}],
            "created_at": _now_iso(),
        }
        self._team_json(team_name).write_text(
            json.dumps(team_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return td

    def load_team(self, team_name: str) -> dict:
        p = self._team_json(team_name)
        if not p.exists():
            raise FileNotFoundError(f"team {team_name!r} not found")
        return json.loads(p.read_text(encoding="utf-8"))

    def register_member(self, team_name: str, agent_name: str) -> None:
        data = self.load_team(team_name)
        names = {m["name"] for m in data["members"]}
        if agent_name not in names:
            data["members"].append({"name": agent_name, "status": STATUS_REGISTERED})
        # Ensure mailbox dir
        (self._team_dir(team_name) / "mailboxes" / agent_name).mkdir(
            parents=True, exist_ok=True
        )
        self._save_team(team_name, data)

    def update_status(self, team_name: str, agent_name: str, status: str) -> None:
        data = self.load_team(team_name)
        for m in data["members"]:
            if m["name"] == agent_name:
                m["status"] = status
                break
        self._save_team(team_name, data)

    def _save_team(self, team_name: str, data: dict) -> None:
        self._team_json(team_name).write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


class Mailbox:
    """File-based message queue for a team."""

    def __init__(self, team_dir: Path):
        self.team_dir = Path(team_dir)

    def _mailbox_dir(self, agent_name: str) -> Path:
        d = self.team_dir / "mailboxes" / agent_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _next_id(self, agent_name: str) -> str:
        d = self._mailbox_dir(agent_name)
        existing = sorted(d.glob("*.msg"))
        if not existing:
            return "001"
        last_num = int(existing[-1].stem)
        return f"{last_num + 1:03d}"

    def send(self, from_name: str, to_name: str, body: str) -> Path:
        mid = self._next_id(to_name)
        msg = {
            "from": from_name,
            "to": to_name,
            "ts": _now_iso(),
            "body": body,
            "consumed": False,
        }
        p = self._mailbox_dir(to_name) / f"{mid}.msg"
        p.write_text(json.dumps(msg, ensure_ascii=False), encoding="utf-8")
        return p

    def peek(self, agent_name: str) -> list[TeamMessage]:
        """Read unconsumed messages without marking them consumed."""
        d = self._mailbox_dir(agent_name)
        messages = []
        for p in sorted(d.glob("*.msg")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("consumed"):
                continue
            messages.append(TeamMessage(
                id=p.stem,
                from_name=data["from"],
                to_name=data["to"],
                ts=data.get("ts", ""),
                body=data["body"],
                consumed=False,
                path=p,
            ))
        return messages

    def ack(self, agent_name: str, message_ids: list[str]) -> None:
        """Mark messages as consumed. Called only after agent completes successfully."""
        d = self._mailbox_dir(agent_name)
        for mid in message_ids:
            p = d / f"{mid}.msg"
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                data["consumed"] = True
                p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except (json.JSONDecodeError, OSError):
                pass

    def unread_count(self, agent_name: str) -> int:
        return len(self.peek(agent_name))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
