"""Approval delivery provenance, readback, dedupe, and migration behavior."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.approval_packets import build_approval_packet, parse_approval_packet


@pytest.fixture
def approval_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.init_db()
    conn = kb.connect(db_path)
    try:
        yield conn, db_path
    finally:
        conn.close()


def _packet(conn) -> dict:
    task_id = kb.create_task(conn, title="Approve release", assignee="worker")
    assert kb.block_task(
        conn,
        task_id,
        kind="needs_input",
        reason="Choose the release path.",
        board_slug="main",
    )
    return kb.get_active_approval_packet(conn, task_id)


def _valid_packet() -> dict:
    return build_approval_packet(
        task_id="t_abcd1234",
        board_slug="main",
        title="Approve release",
        reason="Choose the release path.",
        block_kind="needs_input",
        event_id=42,
        event_kind="blocked",
        now=1_700_000_000,
        packet_id="appr_fixture",
        generation=1,
    )


def test_duplicate_delivery_claim_is_deduped(approval_db):
    conn, _ = approval_db
    packet = _packet(conn)
    route = {
        "packet_id": packet["packet_id"],
        "platform": "telegram",
        "chat_id": "chat-1",
        "thread_id": "topic-7",
    }

    assert kb.begin_approval_delivery(conn, **route) is True
    assert kb.begin_approval_delivery(conn, **route) is False

    kb.finish_approval_delivery(
        conn,
        **route,
        text_message_id="msg-1",
        media_status="delivered",
        media_message_id="photo-1",
    )
    assert kb.begin_approval_delivery(conn, **route) is False
    rows = kb.list_approval_deliveries(conn, packet_id=packet["packet_id"])
    assert len(rows) == 1
    assert rows[0]["status"] == "delivered"


def test_failed_delivery_can_retry_without_duplicate_row(approval_db):
    conn, _ = approval_db
    packet = _packet(conn)
    route = {
        "packet_id": packet["packet_id"],
        "platform": "telegram",
        "chat_id": "chat-1",
        "thread_id": "",
    }
    assert kb.begin_approval_delivery(conn, **route)
    kb.fail_approval_delivery(conn, **route, reason="temporary send failure")

    assert kb.begin_approval_delivery(conn, **route) is True
    assert len(kb.list_approval_deliveries(conn, packet_id=packet["packet_id"])) == 1


def test_delivery_and_readback_are_not_decision_receipt(approval_db):
    conn, _ = approval_db
    packet = _packet(conn)
    route = {
        "packet_id": packet["packet_id"],
        "platform": "desktop",
        "chat_id": "session-1",
        "thread_id": "",
    }
    assert kb.begin_approval_delivery(conn, **route)
    kb.finish_approval_delivery(
        conn, **route, text_message_id=None, media_status="not_supported"
    )
    assert kb.mark_approval_read(conn, **route) is True

    delivery = kb.list_approval_deliveries(conn, packet_id=packet["packet_id"])[0]
    stored = kb.get_active_approval_packet(conn, packet["task_id"])
    assert delivery["delivered_at"] is not None
    assert delivery["read_at"] is not None
    assert stored["provenance"]["status"] == "open"
    assert stored["provenance"]["decision"] is None


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        {},
        {"schema_version": "approval_packet.v2"},
        {"schema_version": "approval_packet.v1", "choices": "A"},
    ],
)
def test_malformed_stored_packet_parsing_fails_closed(raw):
    with pytest.raises(ValueError):
        parse_approval_packet(raw)


def test_packet_parser_rejects_false_redaction_attestation():
    packet = _valid_packet()
    packet["redaction_attestations"]["pii_redacted"] = False

    with pytest.raises(ValueError, match="redaction attestations"):
        parse_approval_packet(packet)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence", [{"kind": "log", "ref": ["not", "text"], "label": "x"}]),
        (
            "attachments",
            [{"id": 1, "filename": "x", "content_type": "text/plain", "size": -1}],
        ),
        ("impact", {"waiting_count": 1, "dependents": "not-a-list"}),
        (
            "reply_syntax",
            {"short": "Reply A/B/C/D", "command": "/kanban decide another A"},
        ),
    ],
)
def test_packet_parser_rejects_malformed_nested_metadata(field, value):
    packet = _valid_packet()
    packet[field] = value

    with pytest.raises(ValueError):
        parse_approval_packet(packet)


def test_packet_parser_rejects_fields_beyond_durable_length_bounds():
    packet = _valid_packet()
    packet["title"] = "x" * 161

    with pytest.raises(ValueError, match="title"):
        parse_approval_packet(packet)


@pytest.mark.parametrize("nested", [None, "choice", "evidence"])
def test_packet_parser_rejects_unknown_fields(nested):
    packet = _valid_packet()
    if nested == "choice":
        packet["choices"][0]["unexpected"] = True
    elif nested == "evidence":
        packet["evidence"][0]["unexpected"] = True
    else:
        packet["unexpected"] = True

    with pytest.raises(ValueError, match="unknown"):
        parse_approval_packet(packet)


@pytest.mark.parametrize("delete_mode", ["direct", "archived"])
def test_task_deletion_removes_approval_rows_and_card(delete_mode, approval_db):
    conn, _ = approval_db
    packet = _packet(conn)
    card_path = Path(packet["provenance"]["card_path"])
    assert card_path.is_file()
    assert kb.begin_approval_delivery(
        conn,
        packet_id=packet["packet_id"],
        platform="telegram",
        chat_id="chat-1",
    )
    kb.finish_approval_delivery(
        conn,
        packet_id=packet["packet_id"],
        platform="telegram",
        chat_id="chat-1",
        media_status="delivered",
    )
    if delete_mode == "archived":
        assert kb.archive_task(conn, packet["task_id"])
        assert kb.delete_archived_task(conn, packet["task_id"])
    else:
        assert kb.delete_task(conn, packet["task_id"])

    assert conn.execute(
        "SELECT COUNT(*) FROM approval_packets WHERE task_id = ?",
        (packet["task_id"],),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM approval_deliveries WHERE packet_id = ?",
        (packet["packet_id"],),
    ).fetchone()[0] == 0
    assert not card_path.exists()


def test_approval_schema_init_and_legacy_recreation_are_idempotent(approval_db):
    conn, db_path = approval_db
    conn.executescript(
        """
        DROP TABLE approval_decision_audit;
        DROP TABLE approval_deliveries;
        DROP TABLE approval_packets;
        """
    )
    conn.close()

    kb.init_db(db_path)
    kb.init_db(db_path)

    check = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'approval_%'"
            )
        }
        assert tables == {
            "approval_decision_audit",
            "approval_deliveries",
            "approval_packets",
        }
        indexes = [
            row[1] for row in check.execute("PRAGMA index_list(approval_packets)")
        ]
        assert len(indexes) == len(set(indexes))
    finally:
        check.close()
