"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None, tenant=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        tenant=tenant,
        triage=True,
    )


def _create_triage_approval(conn) -> tuple[str, dict]:
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
    assert kb.get_task(conn, task_id).status == "triage"
    return task_id, packet


def _arrange_decided_choice(conn, packet: dict, *, action: str) -> None:
    """Arrange the durable state produced by a resolved packet choice."""
    choice = "A" if action == "decompose" else "B"
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE approval_packets SET status = 'decided', "
            "decision_choice = ?, decision_actor = 'operator' "
            "WHERE packet_id = ?",
            (choice, packet["packet_id"]),
        )
        kb._append_event(
            conn,
            packet["task_id"],
            "approval_decided",
            {
                "packet_id": packet["packet_id"],
                "choice": choice,
                "action": action,
            },
        )
        if action == "decompose":
            kb._append_event(
                conn,
                packet["task_id"],
                "decomposition_requested",
                {
                    "packet_id": packet["packet_id"],
                    "generation": packet["freshness"]["generation"],
                    "choice": choice,
                },
            )


def test_decompose_creates_children_and_promotes_root(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")
        assert kb.get_task(conn, tid).status == "triage"

    children = [
        {"title": "research", "body": "look at prior art", "assignee": "researcher", "parents": []},
        {"title": "build it", "body": "write code", "assignee": "engineer", "parents": [0]},
    ]
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=children,
            author="decomposer",
        )
    assert child_ids is not None
    assert len(child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    # Root flipped to todo with orchestrator assignee, gated by children.
    assert root.status == "todo"
    assert root.assignee == "orchestrator"
    # First child has no internal parents → ready on recompute_ready.
    assert c0.status == "ready"
    assert c0.assignee == "researcher"
    # Second child has parents=[0] → stays in todo until c0 completes.
    assert c1.status == "todo"
    assert c1.assignee == "engineer"


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is not None

    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)

    assert any("Decomposed into" in (c.body or "") for c in comments)
    assert any(ev.kind == "decomposed" for ev in events)


def test_auto_decomposer_rejects_resolved_triage_without_decompose_intent(
    kanban_home,
):
    with kb.connect() as conn:
        task_id, packet = _create_triage_approval(conn)
        _arrange_decided_choice(conn, packet, action="keep_blocked")
        before_ids = [task.id for task in kb.list_tasks(conn)]
        before_root = kb.get_task(conn, task_id)

        child_ids = kb.decompose_triage_task(
            conn,
            task_id,
            root_assignee="orchestrator",
            children=[{"title": "unsafe duplicate child", "parents": []}],
            author="auto-decomposer",
        )

        assert child_ids is None
        assert [task.id for task in kb.list_tasks(conn)] == before_ids
        after_root = kb.get_task(conn, task_id)
        assert after_root.status == before_root.status == "triage"
        assert after_root.assignee == before_root.assignee
        assert not any(
            event.kind == "decomposed" for event in kb.list_events(conn, task_id)
        )


def test_captured_auto_decompose_rejects_intent_from_older_generation(
    kanban_home,
):
    with kb.connect() as conn:
        task_id, old_packet = _create_triage_approval(conn)
        _arrange_decided_choice(conn, old_packet, action="decompose")

        # Deterministic interleaving: the auto path captured the old intent,
        # then a newer ordinary keep-blocked generation resolved before its
        # mutation landed.
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?",
            (task_id,),
        )
        assert kb.block_task(
            conn,
            task_id,
            kind="needs_input",
            reason="A newer ordinary owner decision supersedes fan-out.",
        )
        current_packet = kb.get_active_approval_packet(conn, task_id)
        assert current_packet is not None
        assert (
            current_packet["freshness"]["generation"]
            > old_packet["freshness"]["generation"]
        )
        _arrange_decided_choice(conn, current_packet, action="keep_blocked")
        before_ids = [task.id for task in kb.list_tasks(conn)]

        child_ids = kb.decompose_triage_task(
            conn,
            task_id,
            root_assignee="orchestrator",
            children=[{"title": "stale duplicate child", "parents": []}],
            author="auto-decomposer",
        )

        assert child_ids is None
        assert [task.id for task in kb.list_tasks(conn)] == before_ids
        assert kb.get_task(conn, task_id).status == "triage"
        assert not any(
            event.kind == "decomposed" for event in kb.list_events(conn, task_id)
        )


def test_current_explicit_decompose_intent_allows_auto_decomposer_control(
    kanban_home,
):
    with kb.connect() as conn:
        task_id, packet = _create_triage_approval(conn)
        _arrange_decided_choice(conn, packet, action="decompose")

        child_ids = kb.decompose_triage_task(
            conn,
            task_id,
            root_assignee="orchestrator",
            children=[{"title": "authorized child", "parents": []}],
            author="auto-decomposer",
        )

        assert child_ids is not None
        assert len(child_ids) == 1
        assert kb.get_task(conn, child_ids[0]) is not None
        assert kb.get_task(conn, task_id).status == "todo"


