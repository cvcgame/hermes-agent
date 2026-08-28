"""Worker-facing structured approval hints on ``kanban_block``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="worker approval", assignee="test-worker")
        kb.claim_task(conn, task_id)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    return task_id


def test_block_schema_exposes_bounded_structured_approval_hints():
    from tools.kanban_tools import KANBAN_BLOCK_SCHEMA

    approval = KANBAN_BLOCK_SCHEMA["parameters"]["properties"]["approval"]
    assert approval["type"] == "object"
    assert approval["additionalProperties"] is False
    choices = approval["properties"]["choices"]
    assert choices["type"] == "array"
    assert choices["maxItems"] == 4
    assert choices["items"]["additionalProperties"] is False
    assert set(choices["items"]["properties"]) >= {
        "id",
        "label",
        "tradeoff",
        "recommended",
        "action",
        "assignee",
    }
    action = choices["items"]["properties"].get("action")
    assert action is not None
    assert set(action["enum"]) == {"resume", "keep_blocked", "decompose"}
    assignee = choices["items"]["properties"].get("assignee")
    assert assignee is not None
    assert assignee["type"] == "string"


def test_worker_explicit_choices_reach_the_durable_packet(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    result = json.loads(
        kt._handle_block({
            "kind": "needs_input",
            "reason": "Choose a rollout.",
            "approval": {
                "decision_question": "Canary or all at once?",
                "completed_state": "Tests are green; no rollout started.",
                "evidence_refs": ["tests:release"],
                "choices": [
                    {
                        "id": "A",
                        "label": "Canary",
                        "tradeoff": "Slower, with limited blast radius.",
                        "recommended": True,
                    },
                    {
                        "id": "B",
                        "label": "All at once",
                        "tradeoff": "Faster, with wider blast radius.",
                        "recommended": False,
                    },
                ],
            },
        })
    )

    assert result["ok"] is True
    conn = kb.connect()
    try:
        packet = kb.get_active_approval_packet(conn, worker_env)
    finally:
        conn.close()
    assert packet["decision_question"] == "Canary or all at once?"
    assert [choice["label"] for choice in packet["choices"]] == [
        "Canary",
        "All at once",
    ]


def test_worker_malformed_choices_fail_closed_before_task_block(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    result = json.loads(
        kt._handle_block({
            "kind": "needs_input",
            "reason": "Choose a rollout.",
            "approval": {
                "choices": [
                    {
                        "id": "A",
                        "label": "Canary",
                        "tradeoff": "Slower.",
                        "recommended": False,
                    }
                ]
            },
        })
    )

    assert "error" in result
    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "running"
        assert kb.list_approval_packets(conn, task_id=worker_env) == []
    finally:
        conn.close()


def test_worker_unknown_approval_fields_fail_closed_before_task_block(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    result = json.loads(
        kt._handle_block({
            "kind": "needs_input",
            "reason": "Choose a rollout.",
            "approval": {"unexpected": "ignored only by a fail-open parser"},
        })
    )

    assert "error" in result
    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "running"
        assert kb.list_approval_packets(conn, task_id=worker_env) == []
    finally:
        conn.close()
