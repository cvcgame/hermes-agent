"""Telegram approval replies are explicit, route-bound, and auditable."""

from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb


def _create_delivered_packet(
    *,
    chat_id: str = "12345",
    board: str | None = None,
    text_message_id: str = "notice-1",
) -> tuple[str, str]:
    conn = kb.connect(board=board)
    try:
        task_id = kb.create_task(
            conn, title="Choose rollout", assignee="worker", board=board
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id=chat_id,
            user_id="111",
        )
        assert kb.block_task(
            conn,
            task_id,
            reason="A rollout mode is required",
            kind="needs_input",
            approval={
                "decision_question": "Which rollout mode?",
                "choices": [
                    {
                        "id": "A",
                        "label": "Staged",
                        "tradeoff": "Slower, lower risk",
                        "recommended": True,
                    },
                    {
                        "id": "B",
                        "label": "Immediate",
                        "tradeoff": "Faster, higher risk",
                        "recommended": False,
                    },
                ],
            },
        )
        packet = kb.get_active_approval_packet(conn, task_id)
        assert packet is not None
        assert kb.begin_approval_delivery(
            conn,
            packet_id=packet["packet_id"],
            platform="telegram",
            chat_id=chat_id,
        )
        kb.finish_approval_delivery(
            conn,
            packet_id=packet["packet_id"],
            platform="telegram",
            chat_id=chat_id,
            text_message_id=text_message_id,
            media_status="delivered",
        )
        return task_id, packet["packet_id"]
    finally:
        conn.close()


def _create_two_delivered_generations(
    *, chat_id: str = "12345"
) -> tuple[str, str, str]:
    task_id, old_packet_id = _create_delivered_packet(chat_id=chat_id)
    conn = kb.connect()
    try:
        assert kb.unblock_task(conn, task_id)
        assert kb.block_task(
            conn,
            task_id,
            reason="A new rollout decision is required",
            kind="needs_input",
        )
        packet = kb.get_active_approval_packet(conn, task_id)
        assert packet is not None
        assert kb.begin_approval_delivery(
            conn,
            packet_id=packet["packet_id"],
            platform="telegram",
            chat_id=chat_id,
        )
        kb.finish_approval_delivery(
            conn,
            packet_id=packet["packet_id"],
            platform="telegram",
            chat_id=chat_id,
            text_message_id="notice-2",
            media_status="delivered",
        )
        return task_id, old_packet_id, packet["packet_id"]
    finally:
        conn.close()


def _task_and_packet_snapshot(
    task_id: str, packet_id: str, *, board: str | None = None
) -> tuple[dict, dict, list, list, list]:
    conn = kb.connect(board=board)
    try:
        packet = next(
            packet
            for packet in kb.list_approval_packets(conn, task_id=task_id)
            if packet["packet_id"] == packet_id
        )
        return (
            asdict(kb.get_task(conn, task_id)),
            packet,
            kb.list_approval_deliveries(conn, packet_id=packet_id),
            [asdict(comment) for comment in kb.list_comments(conn, task_id)],
            [asdict(event) for event in kb.list_events(conn, task_id)],
        )
    finally:
        conn.close()


def _configure_isolated_boards(tmp_path, monkeypatch) -> None:
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(name, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.create_board("alpha")
    kb.create_board("beta")


def _approval_audit(task_id: str, *, board: str) -> list[dict]:
    conn = kb.connect(board=board)
    try:
        return kb.list_approval_decision_audit(conn, task_id=task_id)
    finally:
        conn.close()


def _source(chat_id: str = "12345", *, user_id: str = "111") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id=user_id,
        chat_id=chat_id,
        user_name="Decision maker",
        chat_type="dm",
    )


@pytest.mark.asyncio
async def test_slash_decide_applies_fresh_choice_and_audits(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "decide.db"))
    kb.init_db()
    task_id, packet_id = _create_delivered_packet()
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    event = MessageEvent(
        text=f"/kanban decide {task_id} A",
        source=_source(),
        message_id="reply-1",
    )

    response = await runner._handle_kanban_command(event)

    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "ready"
        audit = kb.list_approval_decision_audit(conn, packet_id=packet_id)
    finally:
        conn.close()
    assert "accepted" in response.lower()
    assert audit[-1]["accepted"] is True
    assert audit[-1]["choice"] == "A"


@pytest.mark.asyncio
async def test_same_chat_non_subscriber_cannot_apply_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "subscriber.db"))
    kb.init_db()
    task_id, packet_id = _create_delivered_packet(chat_id="group-1")
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    event = MessageEvent(
        text=f"/kanban decide {task_id} A",
        source=_source("group-1", user_id="222"),
        message_id="reply-other-member",
    )

    response = await runner._handle_kanban_command(event)

    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "blocked"
        audit = kb.list_approval_decision_audit(conn, packet_id=packet_id)
    finally:
        conn.close()
    assert "rejected" in response.lower()
    assert audit[-1]["accepted"] is False
    assert audit[-1]["reason"] == "reply sender is not the task subscriber"


def _telegram_adapter():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="fake-token", extra={})
    adapter._bot = SimpleNamespace(id=999, username="test_bot")
    adapter._message_handler = AsyncMock()
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 0.01
    adapter._text_batch_split_delay_seconds = 0.01
    adapter._mention_patterns = adapter._compile_mention_patterns()
    adapter._forum_lock = __import__("asyncio").Lock()
    adapter._forum_command_registered = set()
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter.send = AsyncMock()
    return adapter


def _telegram_update(
    text: str,
    *,
    chat_id: int = 12345,
    reply_to_message_id: str | None = None,
):
    reply_to_message = None
    if reply_to_message_id is not None:
        reply_to_message = SimpleNamespace(
            message_id=reply_to_message_id,
            text="Approval required",
            caption=None,
            from_user=SimpleNamespace(id=999, username="test_bot"),
            photo=None,
            video=None,
            voice=None,
            audio=None,
            document=None,
        )
    msg = SimpleNamespace(
        message_id=42,
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(id=chat_id, type="private", title=None, is_forum=False),
        from_user=SimpleNamespace(
            id=111, full_name="Decision maker", first_name="Decision"
        ),
        reply_to_message=reply_to_message,
        date=None,
        location=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
        sticker=None,
        media_group_id=None,
    )
    return SimpleNamespace(update_id=1, message=msg, effective_message=None)


@pytest.mark.asyncio
async def test_reply_to_superseded_telegram_notice_is_rejected_without_mutation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "old-reply.db"))
    kb.init_db()

    # The same subscriber receives two generations on the same Telegram route.
    task_id, old_packet_id, current_packet_id = _create_two_delivered_generations()

    adapter = _telegram_adapter()
    before_old_reply = _task_and_packet_snapshot(task_id, current_packet_id)
    await adapter._handle_text_message(
        _telegram_update("A", reply_to_message_id="notice-1"),
        SimpleNamespace(),
    )

    conn = kb.connect()
    try:
        after_old_reply = _task_and_packet_snapshot(task_id, current_packet_id)
        old_audit = kb.list_approval_decision_audit(conn, task_id=task_id)
    finally:
        conn.close()
    assert after_old_reply == before_old_reply
    assert len(old_audit) == 1
    assert old_audit[0]["packet_id"] == old_packet_id
    assert old_audit[0]["accepted"] is False
    assert "superseded" in old_audit[0]["reason"]
    assert "rejected" in adapter.send.await_args.args[1].lower()
    adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_slash_reply_to_superseded_notice_is_rejected_without_mutation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "old-slash-reply.db"))
    kb.init_db()
    task_id, old_packet_id, current_packet_id = _create_two_delivered_generations()
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    before = _task_and_packet_snapshot(task_id, current_packet_id)

    response = await runner._handle_kanban_command(
        MessageEvent(
            text=f"/kanban decide {task_id} A",
            source=_source(),
            message_id="slash-reply-old-notice",
            reply_to_message_id="notice-1",
        )
    )

    conn = kb.connect()
    try:
        after = _task_and_packet_snapshot(task_id, current_packet_id)
        audit = kb.list_approval_decision_audit(conn, task_id=task_id)
    finally:
        conn.close()
    assert after == before
    assert len(audit) == 1
    assert audit[0]["packet_id"] == old_packet_id
    assert audit[0]["accepted"] is False
    assert "superseded" in audit[0]["reason"]
    assert "superseded" in response


@pytest.mark.asyncio
async def test_standalone_decide_is_ambiguous_after_two_route_generations(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "ambiguous.db"))
    kb.init_db()
    task_id, _, current_packet_id = _create_two_delivered_generations()

    # With no Telegram reply anchor, the explicit task syntax is still
    # ambiguous because both generations were delivered to this route.
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    before_standalone = _task_and_packet_snapshot(task_id, current_packet_id)
    standalone_response = await runner._handle_kanban_command(
        MessageEvent(
            text=f"/kanban decide {task_id} A",
            source=_source(),
            message_id="standalone-command",
        )
    )

    conn = kb.connect()
    try:
        after_standalone = _task_and_packet_snapshot(task_id, current_packet_id)
        standalone_audit = kb.list_approval_decision_audit(conn, task_id=task_id)
    finally:
        conn.close()
    assert after_standalone == before_standalone
    assert len(standalone_audit) == 1
    assert standalone_audit[-1]["packet_id"] == current_packet_id
    assert standalone_audit[-1]["accepted"] is False
    assert "ambiguous" in standalone_audit[-1]["reason"]
    assert "ambiguous" in standalone_response.lower()


@pytest.mark.asyncio
async def test_reply_to_current_telegram_notice_remains_supported(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "current-reply.db"))
    kb.init_db()
    task_id, old_packet_id, current_packet_id = _create_two_delivered_generations()

    # Exact provenance for the newest notice remains a supported path for the
    # same subscribed sender; the decision never enters the agent prompt.
    adapter = _telegram_adapter()
    await adapter._handle_text_message(
        _telegram_update("A", reply_to_message_id="notice-2"),
        SimpleNamespace(),
    )

    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "ready"
        packets = {
            packet["packet_id"]: packet
            for packet in kb.list_approval_packets(conn, task_id=task_id)
        }
        current_audit = kb.list_approval_decision_audit(conn, task_id=task_id)
    finally:
        conn.close()
    assert packets[old_packet_id]["provenance"]["status"] == "superseded"
    assert packets[current_packet_id]["provenance"]["status"] == "decided"
    assert current_audit[-1]["packet_id"] == current_packet_id
    assert current_audit[-1]["accepted"] is True
    assert "accepted" in adapter.send.await_args.args[1].lower()
    adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_unanchored_plain_choice_is_ambiguous_across_boards(
    tmp_path, monkeypatch
):
    _configure_isolated_boards(tmp_path, monkeypatch)
    contexts = {
        board: _create_delivered_packet(
            board=board, text_message_id=f"{board}-notice"
        )
        for board in ("alpha", "beta")
    }
    before = {
        board: _task_and_packet_snapshot(task_id, packet_id, board=board)
        for board, (task_id, packet_id) in contexts.items()
    }
    adapter = _telegram_adapter()

    await adapter._handle_text_message(_telegram_update("A"), SimpleNamespace())

    adapter._message_handler.assert_not_awaited()
    assert adapter._pending_text_batches == {}
    after = {
        board: _task_and_packet_snapshot(task_id, packet_id, board=board)
        for board, (task_id, packet_id) in contexts.items()
    }
    for board in contexts:
        assert after[board] == before[board], f"{board} board was mutated"

    audits = [
        audit
        for board, (task_id, _) in contexts.items()
        for audit in _approval_audit(task_id, board=board)
    ]
    assert audits
    assert all(not audit["accepted"] for audit in audits)
    assert all("ambiguous" in audit["reason"] for audit in audits)
    assert "ambiguous" in adapter.send.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_unanchored_task_id_collision_is_ambiguous_across_boards(
    tmp_path, monkeypatch
):
    _configure_isolated_boards(tmp_path, monkeypatch)
    shared_task_id = "t_a11ce"
    monkeypatch.setattr(kb, "_new_task_id", lambda: shared_task_id)
    contexts = {
        board: _create_delivered_packet(
            board=board, text_message_id=f"{board}-collision-notice"
        )
        for board in ("alpha", "beta")
    }
    assert {task_id for task_id, _ in contexts.values()} == {shared_task_id}
    before = {
        board: _task_and_packet_snapshot(task_id, packet_id, board=board)
        for board, (task_id, packet_id) in contexts.items()
    }
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    response = await runner._handle_kanban_command(
        MessageEvent(
            text=f"/kanban decide {shared_task_id} A",
            source=_source(),
            message_id="colliding-task-command",
        )
    )

    after = {
        board: _task_and_packet_snapshot(task_id, packet_id, board=board)
        for board, (task_id, packet_id) in contexts.items()
    }
    for board in contexts:
        assert after[board] == before[board], f"{board} board was mutated"

    audits = [
        audit
        for board, (task_id, _) in contexts.items()
        for audit in _approval_audit(task_id, board=board)
    ]
    assert audits
    assert all(not audit["accepted"] for audit in audits)
    assert all("ambiguous" in audit["reason"] for audit in audits)
    assert "ambiguous" in response.lower()


@pytest.mark.asyncio
async def test_exact_reply_provenance_selects_intended_board(
    tmp_path, monkeypatch
):
    _configure_isolated_boards(tmp_path, monkeypatch)
    contexts = {
        board: _create_delivered_packet(
            board=board, text_message_id=f"{board}-exact-notice"
        )
        for board in ("alpha", "beta")
    }
    alpha_task_id, alpha_packet_id = contexts["alpha"]
    beta_task_id, beta_packet_id = contexts["beta"]
    alpha_before = _task_and_packet_snapshot(
        alpha_task_id, alpha_packet_id, board="alpha"
    )
    adapter = _telegram_adapter()

    await adapter._handle_text_message(
        _telegram_update("A", reply_to_message_id="beta-exact-notice"),
        SimpleNamespace(),
    )

    assert (
        _task_and_packet_snapshot(alpha_task_id, alpha_packet_id, board="alpha")
        == alpha_before
    )
    beta_conn = kb.connect(board="beta")
    try:
        assert kb.get_task(beta_conn, beta_task_id).status == "ready"
        beta_packet = next(
            packet
            for packet in kb.list_approval_packets(beta_conn, task_id=beta_task_id)
            if packet["packet_id"] == beta_packet_id
        )
    finally:
        beta_conn.close()
    assert beta_packet["provenance"]["status"] == "decided"
    assert _approval_audit(alpha_task_id, board="alpha") == []
    assert _approval_audit(beta_task_id, board="beta")[-1]["accepted"] is True
    assert "accepted" in adapter.send.await_args.args[1].lower()
    adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_choice_uses_active_telegram_context_without_agent_prompt(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "plain.db"))
    kb.init_db()
    task_id, packet_id = _create_delivered_packet()
    adapter = _telegram_adapter()

    await adapter._handle_text_message(_telegram_update("B"), SimpleNamespace())

    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "ready"
        audit = kb.list_approval_decision_audit(conn, packet_id=packet_id)
    finally:
        conn.close()
    assert audit[-1]["accepted"] is True
    assert audit[-1]["choice"] == "B"
    adapter.send.assert_awaited_once()
    assert adapter._pending_text_batches == {}


@pytest.mark.asyncio
async def test_plain_choice_outside_active_context_preserves_normal_telegram_behavior(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "no-context.db"))
    kb.init_db()
    adapter = _telegram_adapter()

    await adapter._handle_text_message(_telegram_update("A"), SimpleNamespace())

    assert len(adapter._pending_text_batches) == 1
    adapter.send.assert_not_awaited()
    for task in adapter._pending_text_batch_tasks.values():
        task.cancel()


@pytest.mark.asyncio
async def test_plain_choice_after_decision_returns_to_normal_telegram_behavior(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "decided-context.db"))
    kb.init_db()
    _create_delivered_packet()
    adapter = _telegram_adapter()
    await adapter._handle_text_message(_telegram_update("A"), SimpleNamespace())
    adapter.send.reset_mock()

    await adapter._handle_text_message(_telegram_update("B"), SimpleNamespace())

    assert len(adapter._pending_text_batches) == 1
    adapter.send.assert_not_awaited()
    for task in adapter._pending_text_batch_tasks.values():
        task.cancel()


@pytest.mark.asyncio
async def test_malformed_slash_choice_is_rejected_audited_and_non_mutating(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "malformed.db"))
    kb.init_db()
    task_id, packet_id = _create_delivered_packet()
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    event = MessageEvent(
        text=f"/kanban decide {task_id} Z",
        source=_source(),
        message_id="reply-bad",
    )

    response = await runner._handle_kanban_command(event)

    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "blocked"
        audit = kb.list_approval_decision_audit(conn, packet_id=packet_id)
    finally:
        conn.close()
    assert "rejected" in response.lower()
    assert audit[-1]["accepted"] is False
    assert audit[-1]["reason"] == "invalid choice"


@pytest.mark.asyncio
async def test_delivered_superseded_packet_is_rejected_without_mutation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "stale.db"))
    kb.init_db()
    task_id, old_packet_id = _create_delivered_packet()
    conn = kb.connect()
    try:
        assert kb.unblock_task(conn, task_id)
        assert kb.block_task(
            conn, task_id, reason="Decision changed", kind="capability"
        )
        new_packet = kb.get_active_approval_packet(conn, task_id)
        before = kb.get_task(conn, task_id).status
    finally:
        conn.close()
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    response = await runner._handle_kanban_command(
        MessageEvent(
            text=f"/kanban decide {task_id} A",
            source=_source(),
            message_id="reply-stale",
        )
    )

    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == before == "blocked"
        assert (
            kb.get_active_approval_packet(conn, task_id)["packet_id"]
            == new_packet["packet_id"]
        )
        audit = kb.list_approval_decision_audit(conn, task_id=task_id)
    finally:
        conn.close()
    assert "superseded" in response
    assert audit[-1]["accepted"] is False
    assert old_packet_id != new_packet["packet_id"]


@pytest.mark.asyncio
async def test_explicit_task_decision_from_wrong_chat_is_rejected_and_audited(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "wrong-route.db"))
    kb.init_db()
    task_id, packet_id = _create_delivered_packet(chat_id="authorized-chat")
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    response = await runner._handle_kanban_command(
        MessageEvent(
            text=f"/kanban decide {task_id} A",
            source=_source(chat_id="different-chat"),
            message_id="wrong-route",
        )
    )

    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id).status == "blocked"
        audit = kb.list_approval_decision_audit(conn, packet_id=packet_id)
    finally:
        conn.close()
    assert "rejected" in response.lower()
    assert audit[-1]["accepted"] is False
    assert audit[-1]["reason"] == "approval not delivered to this route"
