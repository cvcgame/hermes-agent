"""Tests for kb.specify_triage_task — the DB-layer atomic promotion
from the triage column to todo. LLM-free by design."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        triage=True,
    )


def _create_resolved_ordinary_triage(conn) -> str:
    task_id = kb.create_task(conn, title="operator-owned root", assignee="worker")
    assert kb.block_task(
        conn,
        task_id,
        kind="needs_input",
        reason="Choose the delivery plan.",
    )
    assert kb.unblock_task(conn, task_id)
    assert kb.block_task(
        conn,
        task_id,
        kind="needs_input",
        reason="Choose the delivery plan again.",
    )
    packet = kb.get_active_approval_packet(conn, task_id)
    assert packet is not None
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE approval_packets SET status = 'decided', "
            "decision_choice = 'B', decision_actor = 'operator' "
            "WHERE packet_id = ?",
            (packet["packet_id"],),
        )
        kb._append_event(
            conn,
            task_id,
            "approval_decided",
            {
                "packet_id": packet["packet_id"],
                "choice": "B",
                "action": "keep_blocked",
            },
        )
    assert kb.get_task(conn, task_id).status == "triage"
    return task_id


def test_specify_promotes_triage_to_todo(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="rough idea")
        assert kb.get_task(conn, tid).status == "triage"
    with kb.connect() as conn:
        ok = kb.specify_triage_task(
            conn,
            tid,
            title="Refined: rough idea",
            body="**Goal**\nDo the thing.",
            author="specifier-bot",
        )
    assert ok is True
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    # No parents → recompute_ready should have flipped it past todo to ready.
    assert task.status == "ready"
    assert task.title == "Refined: rough idea"
    assert "**Goal**" in (task.body or "")


def test_specify_rejects_blank_title(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="rough")
    with kb.connect() as conn, pytest.raises(ValueError):
        kb.specify_triage_task(conn, tid, title="   ", body="ok")


def test_specify_records_audit_comment_only_when_author_given(kanban_home):
    # With author → comment added.
    with kb.connect() as conn:
        tid1 = _create_triage(conn, title="a")
        kb.specify_triage_task(
            conn, tid1, title="A-spec", body="b", author="ace"
        )
        comments1 = kb.list_comments(conn, tid1)
    assert len(comments1) == 1
    assert "Specified" in comments1[0].body
    assert comments1[0].author == "ace"

    # Without author → no comment (silent).
    with kb.connect() as conn:
        tid2 = _create_triage(conn, title="b")
        kb.specify_triage_task(conn, tid2, title="B-spec", body="b")
        comments2 = kb.list_comments(conn, tid2)
    assert comments2 == []


def test_auto_specifier_rejects_resolved_triage_without_decompose_intent(
    kanban_home,
):
    with kb.connect() as conn:
        task_id = _create_resolved_ordinary_triage(conn)
        before = kb.get_task(conn, task_id)
        before_comments = kb.list_comments(conn, task_id)
        before_events = kb.list_events(conn, task_id)

        ok = kb.specify_triage_task(
            conn,
            task_id,
            title="Unsafe automatic rewrite",
            body="This update came from a stale automatic candidate.",
            assignee="auto-owner",
            author="auto-decomposer",
        )

        assert ok is False
        after = kb.get_task(conn, task_id)
        assert after.status == before.status == "triage"
        assert after.title == before.title
        assert after.body == before.body
        assert after.assignee == before.assignee
        assert kb.list_comments(conn, task_id) == before_comments
        assert kb.list_events(conn, task_id) == before_events
