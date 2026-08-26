"""Approval Packet v1 domain behavior for actionable Kanban blockers."""

from __future__ import annotations

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


def _task(conn, title: str = "Choose a release strategy") -> str:
    return kb.create_task(conn, title=title, assignee="worker")


def _packets(conn, task_id: str) -> list[dict]:
    return kb.list_approval_packets(conn, task_id=task_id)


def test_dependency_wait_is_silent_and_creates_no_approval_packet(approval_db):
    task_id = _task(approval_db)

    assert kb.block_task(
        approval_db,
        task_id,
        kind="dependency",
        reason="Waiting for the schema task to finish.",
        board_slug="main",
    )

    assert kb.get_task(approval_db, task_id).status == "todo"
    assert _packets(approval_db, task_id) == []
    assert [event.kind for event in kb.list_events(approval_db, task_id)][
        -1
    ] == "dependency_wait"


@pytest.mark.parametrize("kind", ["needs_input", "capability"])
def test_actionable_block_creates_one_packet(kind, approval_db):
    task_id = _task(approval_db)

    assert kb.block_task(
        approval_db,
        task_id,
        kind=kind,
        reason="Which rollout path should the worker use?",
        board_slug="release-board",
    )

    packets = _packets(approval_db, task_id)
    assert len(packets) == 1
    packet = packets[0]
    assert packet["schema_version"] == "approval_packet.v1"
    assert packet["task_id"] == task_id
    assert packet["board_slug"] == "release-board"
    assert packet["title"] == "Choose a release strategy"
    assert packet["decision_question"]
    assert packet["why_blocked"] == "Which rollout path should the worker use?"
    assert packet["completed_state"]
    created_at = packet["freshness"]["created_at"]
    assert created_at > 0
    assert packet["freshness"] == {"created_at": created_at, "generation": 1}
    assert packet["reply_syntax"]["short"] == "Reply A/B/C/D"
    assert task_id in packet["reply_syntax"]["command"]
    assert packet["redaction_attestations"] == {
        "bounded": True,
        "pii_redacted": True,
        "secrets_redacted": True,
    }


def test_block_loop_detected_creates_one_packet_for_the_actionable_event(approval_db):
    task_id = _task(approval_db)
    assert kb.block_task(
        approval_db,
        task_id,
        kind="needs_input",
        reason="Choose the migration window.",
        board_slug="main",
    )
    assert kb.unblock_task(approval_db, task_id)

    assert kb.block_task(
        approval_db,
        task_id,
        kind="needs_input",
        reason="Choose the migration window.",
        board_slug="main",
    )

    loop_events = [
        event
        for event in kb.list_events(approval_db, task_id)
        if event.kind == "block_loop_detected"
    ]
    packets = _packets(approval_db, task_id)
    assert [packet["freshness"]["generation"] for packet in packets] == [1, 2]
    assert len(loop_events) == 1
    assert (
        len([
            packet
            for packet in packets
            if packet["provenance"]["event_id"] == loop_events[0].id
        ])
        == 1
    )
    assert packets[-1]["provenance"]["event_kind"] == "block_loop_detected"
    assert kb.get_task(approval_db, task_id).status == "triage"


def test_explicit_choices_require_exactly_one_recommendation(approval_db):
    task_id = _task(approval_db)
    approval = {
        "decision_question": "Deploy now or wait for the maintenance window?",
        "completed_state": "Build and tests are complete; no deployment was started.",
        "evidence_refs": [
            {"kind": "test", "ref": "tests/release.txt", "label": "Release test report"}
        ],
        "choices": [
            {
                "id": "A",
                "label": "Deploy now",
                "tradeoff": "Fastest, with higher on-call risk.",
                "recommended": False,
            },
            {
                "id": "B",
                "label": "Wait for the window",
                "tradeoff": "Slower, with the staffed rollback window available.",
                "recommended": True,
            },
        ],
    }

    assert kb.block_task(
        approval_db,
        task_id,
        kind="needs_input",
        reason="Deployment timing requires an operator decision.",
        approval=approval,
        board_slug="main",
    )

    packet = _packets(approval_db, task_id)[0]
    assert [choice["id"] for choice in packet["choices"]] == ["A", "B"]
    assert sum(choice["recommended"] is True for choice in packet["choices"]) == 1
    assert packet["choices"][1]["tradeoff"].startswith("Slower")
    assert packet["evidence"] == approval["evidence_refs"]


def test_choice_action_and_resume_assignee_are_preserved_in_packet(approval_db):
    task_id = _task(approval_db)
    approval = {
        "choices": [
            {
                "id": "A",
                "label": "Resume with the release operator",
                "tradeoff": "Transfers ownership before work resumes.",
                "recommended": True,
                "action": "resume",
                "assignee": "release-operator",
            }
        ]
    }

    try:
        blocked = kb.block_task(
            approval_db,
            task_id,
            kind="needs_input",
            reason="Choose the release owner.",
            approval=approval,
            board_slug="main",
        )
    except ValueError as exc:
        pytest.fail(f"valid approval choice action/assignee was rejected: {exc}")

    assert blocked is True
    packet = _packets(approval_db, task_id)[0]
    assert packet["choices"][0]["action"] == "resume"
    assert packet["choices"][0]["assignee"] == "release-operator"


def test_no_choice_fallback_is_conservative_and_uses_available_metadata(approval_db):
    task_id = _task(approval_db)
    comment_id = kb.add_comment(
        approval_db, task_id, "operator", "The rollback owner is not assigned yet."
    )
    attachment_id = kb.add_attachment(
        approval_db,
        task_id,
        filename="release-plan.pdf",
        stored_path="/durable/release-plan.pdf",
        content_type="application/pdf",
        size=1234,
        uploaded_by="worker",
    )

    assert kb.block_task(
        approval_db,
        task_id,
        kind="capability",
        reason="A human must assign the rollback owner.",
        board_slug="main",
    )

    packet = _packets(approval_db, task_id)[0]
    assert 2 <= len(packet["choices"]) <= 4
    assert sum(choice["recommended"] is True for choice in packet["choices"]) == 1
    recommended = next(choice for choice in packet["choices"] if choice["recommended"])
    assert recommended["label"] == "Keep the task blocked"
    assert all(choice["label"] and choice["tradeoff"] for choice in packet["choices"])
    assert any(item["ref"] == f"comment:{comment_id}" for item in packet["evidence"])
    assert packet["attachments"] == [
        {
            "content_type": "application/pdf",
            "filename": "release-plan.pdf",
            "id": attachment_id,
            "size": 1234,
        }
    ]


def test_missing_evidence_uses_only_an_explicit_absence_marker(approval_db):
    task_id = _task(approval_db)

    assert kb.block_task(
        approval_db,
        task_id,
        kind="needs_input",
        reason="Choose whether to continue.",
        board_slug="main",
    )

    packet = _packets(approval_db, task_id)[0]
    assert packet["evidence"] == [
        {
            "kind": "task_event",
            "label": "Blocking event; no external evidence was supplied.",
            "ref": f"event:{packet['provenance']['event_id']}",
        }
    ]
    assert "passed" not in packet["completed_state"].lower()
    assert "complete" not in packet["completed_state"].lower()


def test_packet_redacts_secrets_pii_and_bounds_every_free_text_field(approval_db):
    secret = "sk-" + "A" * 32
    email = "person@example.com"
    phone = "+1 415 555 0123"
    long_tail = "z" * 5000
    task_id = _task(approval_db, title=f"Release for {email} {secret} {long_tail}")

    assert kb.block_task(
        approval_db,
        task_id,
        kind="needs_input",
        reason=f"Call {phone}; token={secret}. {long_tail}",
        approval={
            "decision_question": f"Ask {email}? {long_tail}",
            "completed_state": f"Stopped safely before using {secret}. {long_tail}",
            "evidence_refs": [
                {"kind": "log", "ref": f"log:{email}", "label": long_tail}
            ],
            "choices": [
                {
                    "id": "A",
                    "label": f"Contact {email} {long_tail}",
                    "tradeoff": f"Leaks {secret} {phone} {long_tail}",
                    "recommended": True,
                }
            ],
        },
        board_slug="main",
    )

    packet = _packets(approval_db, task_id)[0]
    rendered = repr(packet)
    assert secret not in rendered
    assert email not in rendered
    assert phone not in rendered
    assert len(packet["title"]) <= 160
    assert len(packet["decision_question"]) <= 300
    assert len(packet["why_blocked"]) <= 600
    assert len(packet["completed_state"]) <= 600
    assert len(packet["choices"][0]["label"]) <= 160
    assert len(packet["choices"][0]["tradeoff"]) <= 300
    assert len(packet["evidence"][0]["ref"]) <= 220
    assert len(packet["evidence"][0]["label"]) <= 220


def test_packet_preserves_printable_unicode_but_drops_control_characters(approval_db):
    task_id = _task(approval_db, title="배포 승인\x00 요청")

    assert kb.block_task(
        approval_db,
        task_id,
        kind="needs_input",
        reason="점검 창에 배포할까요?\x07",
        board_slug="릴리스",
    )

    packet = _packets(approval_db, task_id)[0]
    assert packet["title"] == "배포 승인 요청"
    assert packet["why_blocked"] == "점검 창에 배포할까요?"


@pytest.mark.parametrize(
    "approval",
    [
        {"choices": "A"},
        {"choices": [{"id": "A", "label": "x", "tradeoff": "y", "recommended": False}]},
        {
            "choices": [
                {"id": "A", "label": "x", "tradeoff": "y", "recommended": True},
                {"id": "C", "label": "z", "tradeoff": "w", "recommended": False},
            ]
        },
        {"decision_question": {"not": "text"}},
    ],
)
def test_malformed_approval_input_fails_closed_without_blocking(approval, approval_db):
    task_id = _task(approval_db)

    with pytest.raises(ValueError):
        kb.block_task(
            approval_db,
            task_id,
            kind="needs_input",
            reason="Need a choice.",
            approval=approval,
            board_slug="main",
        )

    assert kb.get_task(approval_db, task_id).status == "ready"
    assert _packets(approval_db, task_id) == []


@pytest.mark.parametrize(
    "choice_fields",
    [
        {"action": "launch"},
        {"action": "keep_blocked", "assignee": "worker"},
        {"action": "decompose", "assignee": "worker"},
        {"action": "resume", "assignee": ""},
        {"action": "resume", "assignee": 123},
    ],
)
def test_invalid_choice_action_assignee_combinations_fail_closed(
    choice_fields, approval_db
):
    task_id = _task(approval_db)
    choice = {
        "id": "A",
        "label": "Operator choice",
        "tradeoff": "Bounded test tradeoff.",
        "recommended": True,
        **choice_fields,
    }

    with pytest.raises(ValueError):
        kb.block_task(
            approval_db,
            task_id,
            kind="needs_input",
            reason="Need a bounded operator choice.",
            approval={"choices": [choice]},
            board_slug="main",
        )

    assert kb.get_task(approval_db, task_id).status == "ready"
    assert _packets(approval_db, task_id) == []
