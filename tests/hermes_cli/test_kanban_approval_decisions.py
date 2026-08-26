"""Explicit, freshness-bound, audited Approval Packet decisions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def approval_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kb.connect()
    try:
        yield conn
    finally:
        conn.close()


def _blocked(conn) -> tuple[str, dict]:
    task_id = kb.create_task(conn, title="Approve release", assignee="worker")
    assert kb.block_task(
        conn,
        task_id,
        kind="needs_input",
        reason="Choose the release path.",
        board_slug="main",
    )
    return task_id, kb.get_active_approval_packet(conn, task_id)


def _triage_blocked(conn) -> tuple[str, dict]:
    """Create the repeated actionable block that lands in triage."""
    task_id, _ = _blocked(conn)
    assert kb.unblock_task(conn, task_id)
    assert kb.block_task(
        conn,
        task_id,
        kind="needs_input",
        reason="Choose the release path again.",
        approval={
            "choices": [
                {
                    "id": "A",
                    "label": "Use the selected operator plan",
                    "tradeoff": "Continues with the operator-selected ownership.",
                    "recommended": True,
                },
                {
                    "id": "B",
                    "label": "Keep the task blocked",
                    "tradeoff": "Leaves the task paused for more evidence.",
                    "recommended": False,
                },
            ]
        },
        board_slug="main",
    )
    packet = kb.get_active_approval_packet(conn, task_id)
    assert packet is not None
    assert kb.get_task(conn, task_id).status == "triage"
    return task_id, packet


def _inject_choice_contract(
    conn,
    packet_id: str,
    *,
    choice: str = "A",
    action: str,
    assignee: str | None = None,
) -> None:
    """Arrange the v1-compatible choice shape without depending on its parser.

    The production parser does not accept the new fields yet.  Updating the
    durable packet lets the decision-boundary test fail through its normal
    result contract (rather than crashing during fixture setup).
    """
    row = conn.execute(
        "SELECT packet_json FROM approval_packets WHERE packet_id = ?",
        (packet_id,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row["packet_json"])
    selected = next(item for item in payload["choices"] if item["id"] == choice)
    selected["action"] = action
    if assignee is not None:
        selected["assignee"] = assignee
    conn.execute(
        "UPDATE approval_packets SET packet_json = ? WHERE packet_id = ?",
        (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            packet_id,
        ),
    )


def _task_snapshot(conn, task_id: str) -> tuple:
    task = kb.get_task(conn, task_id)
    return (
        task.status,
        task.block_kind,
        task.block_recurrences,
        len(kb.list_comments(conn, task_id)),
        len(kb.list_events(conn, task_id)),
    )


def _internal_authorizer(conn, packet_id: str) -> str:
    row = conn.execute(
        "SELECT freshness_token FROM approval_packets WHERE packet_id = ?",
        (packet_id,),
    ).fetchone()
    assert row is not None
    return row["freshness_token"]


def test_fresh_explicit_decision_is_applied_and_audited(approval_db):
    task_id, packet = _blocked(approval_db)

    result = kb.apply_approval_decision(
        approval_db,
        task_id=task_id,
        choice="A",
        freshness_token=_internal_authorizer(approval_db, packet["packet_id"]),
        actor="telegram:operator-1",
        platform="telegram",
        chat_id="chat-1",
    )

    assert result == {
        "accepted": True,
        "choice": "A",
        "packet_id": packet["packet_id"],
        "reason": "decision applied",
        "task_mutated": True,
    }
    assert kb.get_task(approval_db, task_id).status == "ready"
    decided = kb.list_approval_packets(approval_db, task_id=task_id)[0]
    assert decided["provenance"]["status"] == "decided"
    assert decided["provenance"]["decision"]["choice"] == "A"
    assert any(
        "Decision A" in comment.body
        for comment in kb.list_comments(approval_db, task_id)
    )
    assert [event.kind for event in kb.list_events(approval_db, task_id)][
        -1
    ] == "approval_decided"
    audit = kb.list_approval_decision_audit(approval_db, task_id=task_id)
    assert len(audit) == 1
    assert audit[0]["accepted"] is True
    assert audit[0]["chat_id"] != "chat-1"
    assert audit[0]["chat_id"].startswith("sha256:")


def test_triage_resume_reassigns_and_promotes_existing_root_without_decomposition(
    approval_db,
):
    task_id, packet = _triage_blocked(approval_db)
    _inject_choice_contract(
        approval_db,
        packet["packet_id"],
        action="resume",
        assignee="release-operator",
    )

    result = kb.apply_approval_decision_for_packet(
        approval_db,
        task_id=task_id,
        packet_id=packet["packet_id"],
        choice="A",
        actor="operator:owner",
        platform="desktop",
        chat_id="local-operator",
    )

    assert result["accepted"] is True, result
    assert result["task_mutated"] is True
    root = kb.get_task(approval_db, task_id)
    assert root.status == "ready"
    assert root.assignee == "release-operator"
    assert [task.id for task in kb.list_tasks(approval_db)] == [task_id]
    assert kb.task_graph_context(approval_db, task_id) == {
        "parents": [],
        "children": [],
    }
    assert not any(
        event.kind in {"decomposition_requested", "decomposed"}
        for event in kb.list_events(approval_db, task_id)
    )
    audit = kb.list_approval_decision_audit(approval_db, task_id=task_id)
    assert len(audit) == 1
    assert audit[0]["accepted"] is True


def test_duplicate_same_packet_choice_is_idempotent_without_second_audit(
    approval_db,
):
    task_id, packet = _triage_blocked(approval_db)
    kwargs = {
        "task_id": task_id,
        "packet_id": packet["packet_id"],
        "choice": "A",
        "actor": "operator:owner",
        "platform": "desktop",
        "chat_id": "local-operator",
    }

    first = kb.apply_approval_decision_for_packet(approval_db, **kwargs)
    assert first["accepted"] is True
    after_first = _task_snapshot(approval_db, task_id)
    second = kb.apply_approval_decision_for_packet(approval_db, **kwargs)

    assert second["task_mutated"] is False
    assert _task_snapshot(approval_db, task_id) == after_first
    assert [task.id for task in kb.list_tasks(approval_db)] == [task_id]
    assert kb.task_graph_context(approval_db, task_id) == {
        "parents": [],
        "children": [],
    }
    audit = kb.list_approval_decision_audit(approval_db, task_id=task_id)
    assert len(audit) == 1
    assert audit[0]["accepted"] is True


def test_same_choice_retry_is_stale_after_a_newer_packet_generation(approval_db):
    task_id, old_packet = _blocked(approval_db)
    first = kb.apply_approval_decision_for_packet(
        approval_db,
        task_id=task_id,
        packet_id=old_packet["packet_id"],
        choice="A",
        actor="operator:owner",
        platform="desktop",
        chat_id="local-operator",
    )
    assert first["accepted"] is True
    assert kb.block_task(
        approval_db,
        task_id,
        kind="needs_input",
        reason="A newer generation now needs a different decision.",
    )
    current_packet = kb.get_active_approval_packet(approval_db, task_id)
    assert current_packet is not None
    assert current_packet["packet_id"] != old_packet["packet_id"]
    before = _task_snapshot(approval_db, task_id)

    retry = kb.apply_approval_decision_for_packet(
        approval_db,
        task_id=task_id,
        packet_id=old_packet["packet_id"],
        choice="A",
        actor="operator:owner",
        platform="desktop",
        chat_id="local-operator",
    )

    assert retry["accepted"] is False
    assert retry["task_mutated"] is False
    assert "superseded" in retry["reason"]
    assert _task_snapshot(approval_db, task_id) == before
    active_after = kb.get_active_approval_packet(approval_db, task_id)
    assert active_after is not None
    assert active_after["packet_id"] == current_packet["packet_id"]
    audit = kb.list_approval_decision_audit(approval_db, task_id=task_id)
    assert len(audit) == 2
    assert audit[0]["accepted"] is True
    assert audit[1]["accepted"] is False
    assert "superseded" in audit[1]["reason"]


def test_explicit_decompose_choice_records_exact_generation_intent_and_stays_triage(
    approval_db,
):
    task_id, packet = _triage_blocked(approval_db)
    _inject_choice_contract(
        approval_db,
        packet["packet_id"],
        action="decompose",
    )

    result = kb.apply_approval_decision_for_packet(
        approval_db,
        task_id=task_id,
        packet_id=packet["packet_id"],
        choice="A",
        actor="operator:owner",
        platform="desktop",
        chat_id="local-operator",
    )

    assert result["accepted"] is True, result
    assert kb.get_task(approval_db, task_id).status == "triage"
    assert [task.id for task in kb.list_tasks(approval_db)] == [task_id]
    intents = [
        event
        for event in kb.list_events(approval_db, task_id)
        if event.kind == "decomposition_requested"
    ]
    assert len(intents) == 1
    assert intents[0].payload["packet_id"] == packet["packet_id"]
    assert intents[0].payload["generation"] == packet["freshness"]["generation"]

    from hermes_cli import kanban_decompose as decomp

    assert decomp.list_triage_ids() == [task_id]
    audit = kb.list_approval_decision_audit(approval_db, task_id=task_id)
    assert len(audit) == 1
    assert audit[0]["accepted"] is True


def test_stale_freshness_token_is_rejected_with_zero_task_mutation(approval_db):
    task_id, old_packet = _blocked(approval_db)
    old_authorizer = _internal_authorizer(approval_db, old_packet["packet_id"])
    assert kb.unblock_task(approval_db, task_id)
    assert kb.block_task(
        approval_db,
        task_id,
        kind="needs_input",
        reason="Choose the release path again.",
        board_slug="main",
    )
    before = _task_snapshot(approval_db, task_id)

    result = kb.apply_approval_decision(
        approval_db,
        task_id=task_id,
        choice="A",
        freshness_token=old_authorizer,
        actor="telegram:operator-1",
        platform="telegram",
        chat_id="chat-1",
    )

    assert result["accepted"] is False
    assert result["reason"] == "stale approval token"
    assert result["task_mutated"] is False
    assert _task_snapshot(approval_db, task_id) == before
    audit = kb.list_approval_decision_audit(approval_db, task_id=task_id)
    assert audit[-1]["accepted"] is False
    assert audit[-1]["reason"] == "stale approval token"


def test_unblocked_packet_cannot_unlock_a_later_non_actionable_block(approval_db):
    task_id, old_packet = _blocked(approval_db)
    old_authorizer = _internal_authorizer(approval_db, old_packet["packet_id"])
    assert kb.unblock_task(approval_db, task_id)
    assert kb.block_task(
        approval_db,
        task_id,
        kind="transient",
        reason="A later retryable infrastructure failure.",
        board_slug="main",
    )
    before = _task_snapshot(approval_db, task_id)

    result = kb.apply_approval_decision(
        approval_db,
        task_id=task_id,
        choice="A",
        freshness_token=old_authorizer,
        actor="telegram:operator-1",
        platform="telegram",
        chat_id="chat-1",
    )

    assert result["accepted"] is False
    assert result["task_mutated"] is False
    assert _task_snapshot(approval_db, task_id) == before
    assert kb.get_active_approval_packet(approval_db, task_id) is None


@pytest.mark.parametrize("choice", ["", "E", "AA", "A; DROP TABLE tasks"])
def test_malformed_choice_is_rejected_without_task_mutation(choice, approval_db):
    task_id, packet = _blocked(approval_db)
    before = _task_snapshot(approval_db, task_id)

    result = kb.apply_approval_decision(
        approval_db,
        task_id=task_id,
        choice=choice,
        freshness_token=_internal_authorizer(approval_db, packet["packet_id"]),
        actor="telegram:operator-1",
        platform="telegram",
        chat_id="chat-1",
    )

    assert result["accepted"] is False
    assert result["reason"] == "invalid choice"
    assert result["task_mutated"] is False
    assert _task_snapshot(approval_db, task_id) == before
