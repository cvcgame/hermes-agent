"""Route-bound parsing and application of Kanban Approval Packet replies."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Optional


_PLAIN_CHOICE_RE = re.compile(r"^\s*([A-D])\s*$", re.IGNORECASE)
_DECIDE_RE = re.compile(
    r"^\s*/?kanban\s+decide\s+(t_[0-9a-f]+)\s+([A-D])\s*$",
    re.IGNORECASE,
)
_DECIDE_ATTEMPT_RE = re.compile(
    r"^\s*/?kanban\s+decide\s+(t_[0-9a-f]+)\s+(\S+)\s*$",
    re.IGNORECASE,
)
_DECIDE_PREFIX_RE = re.compile(r"^\s*/?kanban\s+decide\b", re.IGNORECASE)


def parse_approval_reply(text: Any) -> Optional[dict[str, Optional[str]]]:
    """Parse only the two supported, deliberately narrow reply forms."""
    if not isinstance(text, str):
        return None
    plain = _PLAIN_CHOICE_RE.fullmatch(text)
    if plain:
        return {"task_id": None, "choice": plain.group(1).upper()}
    command = _DECIDE_RE.fullmatch(text)
    if command:
        return {
            "task_id": command.group(1).lower(),
            "choice": command.group(2).upper(),
        }
    return None


def is_malformed_decide_command(text: Any) -> bool:
    return isinstance(text, str) and bool(_DECIDE_PREFIX_RE.match(text))


def _platform_name(source) -> str:
    platform = getattr(source, "platform", "")
    return str(getattr(platform, "value", platform) or "").lower()


def _safe_actor(source) -> str:
    platform = _platform_name(source) or "unknown"
    user_id = str(getattr(source, "user_id", "") or "anonymous")
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
    return f"{platform}:user:{digest}"


def _board_slugs(kb, requested_board: Optional[str]) -> list[str]:
    if requested_board:
        return [requested_board]
    try:
        return [
            (meta or {}).get("slug") or kb.DEFAULT_BOARD
            for meta in kb.list_boards(include_archived=False)
        ]
    except Exception:
        return [kb.DEFAULT_BOARD]


def handle_approval_reply(
    text: Any,
    *,
    source,
    requested_board: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
) -> Optional[str]:
    """Apply an explicit reply only within its delivered approval context.

    ``None`` means the message is not an approval reply for this route and the
    caller must preserve its normal behavior.  A string is a user-visible,
    auditable acceptance or rejection and must not be sent to the agent.
    """
    parsed = parse_approval_reply(text)
    if parsed is None:
        if is_malformed_decide_command(text):
            attempt = _DECIDE_ATTEMPT_RE.fullmatch(str(text))
            if attempt is None:
                return "Rejected: use /kanban decide <task-id> <A|B|C|D>. No task was changed."
            # Retain a resolvable task/route so apply_approval_decision can
            # record this invalid choice as a rejected, zero-mutation attempt.
            parsed = {
                "task_id": attempt.group(1).lower(),
                "choice": attempt.group(2),
            }
        else:
            return None

    platform = _platform_name(source)
    chat_id = str(getattr(source, "chat_id", "") or "")
    thread_id = str(getattr(source, "thread_id", "") or "")
    reply_anchor = (
        str(reply_to_message_id) if reply_to_message_id is not None else None
    )
    if not platform or not chat_id:
        return (
            "Rejected: this reply has no stable delivery context. No task was changed."
        )

    from hermes_cli import kanban_db as kb

    connections: dict[str, tuple[Any, str]] = {}
    contexts: list[tuple[dict[str, Any], Any, str]] = []
    rejection_target: Optional[tuple[Any, str]] = None
    try:
        for slug in _board_slugs(kb, requested_board):
            try:
                path = str(Path(kb.kanban_db_path(slug)).resolve())
            except Exception:
                continue
            if path in connections:
                continue
            try:
                conn = kb.connect(board=slug)
            except Exception:
                continue
            connections[path] = (conn, slug)
            try:
                matches = kb.find_approval_delivery_contexts(
                    conn,
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    task_id=parsed["task_id"],
                    reply_to_message_id=reply_anchor,
                )
            except ValueError:
                # Durable malformed packet: fail closed without falling through
                # to another task or treating a plain A/B/C/D as an agent prompt.
                return "Rejected: the delivered approval packet is malformed. No task was changed."

            if not matches and parsed["task_id"] is not None and rejection_target is None:
                try:
                    if kb.get_active_approval_packet(conn, parsed["task_id"]):
                        rejection_target = (conn, slug)
                except ValueError:
                    return "Rejected: the active approval packet is malformed. No task was changed."

            for found in matches:
                if (
                    reply_anchor is None
                    and parsed["task_id"] is None
                    and found["provenance"]["status"] != "open"
                ):
                    # Bare A/B/C/D is only special while this route has an active
                    # packet. Once decided/superseded it returns to normal chat.
                    continue
                contexts.append((found, conn, slug))

        if len(contexts) > 1:
            # Resolve every eligible board before writing anything. Audit once
            # per matching board/task, retaining the newest generation selected
            # by find_approval_delivery_contexts for same-task ambiguity.
            audited: set[tuple[int, str]] = set()
            for ambiguous, conn, _slug in contexts:
                audit_key = (id(conn), ambiguous["task_id"])
                if audit_key in audited:
                    continue
                audited.add(audit_key)
                kb.record_approval_decision_rejection(
                    conn,
                    task_id=ambiguous["task_id"],
                    packet_id=ambiguous["packet_id"],
                    choice=parsed["choice"],
                    actor=_safe_actor(source),
                    platform=platform,
                    chat_id=chat_id,
                    reason="ambiguous delivered approval generations",
                )
            return (
                "Rejected: the delivered approval context is ambiguous; reply "
                "directly to the intended notice. No task was changed."
            )

        if not contexts:
            if parsed["task_id"] is None:
                return None
            if rejection_target is not None:
                conn, _slug = rejection_target
                kb.record_approval_decision_rejection(
                    conn,
                    task_id=parsed["task_id"],
                    choice=parsed["choice"],
                    actor=_safe_actor(source),
                    platform=platform,
                    chat_id=chat_id,
                    reason="approval not delivered to this route",
                )
            return "Rejected: no approval for that task was delivered in this chat. No task was changed."

        context, context_conn, context_slug = contexts[0]
        source_user_id = str(getattr(source, "user_id", "") or "") or None
        source_user_id_alt = str(getattr(source, "user_id_alt", "") or "") or None
        if not kb.approval_reply_sender_matches_subscriber(
            context_conn,
            task_id=context["task_id"],
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=source_user_id,
            user_id_alt=source_user_id_alt,
        ):
            kb.record_approval_decision_rejection(
                context_conn,
                task_id=context["task_id"],
                choice=parsed["choice"],
                actor=_safe_actor(source),
                platform=platform,
                chat_id=chat_id,
                reason="reply sender is not the task subscriber",
                packet_id=context["packet_id"],
            )
            return (
                "Rejected: only the task subscriber can decide this approval. "
                "No task was changed."
            )

        result = kb.apply_approval_decision_for_packet(
            context_conn,
            task_id=context["task_id"],
            packet_id=context["packet_id"],
            choice=parsed["choice"],
            actor=_safe_actor(source),
            platform=platform,
            chat_id=chat_id,
        )
        kb.mark_approval_read(
            context_conn,
            packet_id=context["packet_id"],
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
        )

        board_suffix = f" on [{context_slug}]" if context_slug else ""
        if result["accepted"]:
            return (
                f"Decision accepted and audited: {context['task_id']} → "
                f"{result['choice']}{board_suffix}."
            )
        return (
            f"Decision rejected and audited for {context['task_id']}: "
            f"{result['reason']}. No task was changed."
        )
    finally:
        for conn, _slug in connections.values():
            conn.close()
