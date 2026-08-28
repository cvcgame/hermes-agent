"""Bounded, sanitized Approval Packet v1 construction and rendering helpers.

This module is deliberately storage-agnostic.  ``kanban_db`` owns durable
rows and passes only already-resolved task/comment/attachment/graph metadata
through this boundary.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Mapping, Sequence
from typing import Any, Optional


SCHEMA_VERSION = "approval_packet.v1"
CHOICE_IDS = tuple("ABCD")
MAX_TITLE = 160
MAX_QUESTION = 300
MAX_REASON = 600
MAX_COMPLETED_STATE = 600
MAX_CHOICE_LABEL = 160
MAX_TRADEOFF = 300
MAX_ASSIGNEE = 64
MAX_EVIDENCE_TEXT = 220
MAX_EVIDENCE = 12
MAX_ATTACHMENTS = 12
MAX_DEPENDENTS = 12
MAX_PACKET_JSON_BYTES = 24_000
_APPROVAL_INPUT_FIELDS = frozenset({
    "decision_question", "completed_state", "evidence_refs", "choices"
})
_PACKET_FIELDS = frozenset({
    "schema_version", "packet_id", "task_id", "board_slug", "title",
    "decision_question", "why_blocked", "block_kind", "completed_state",
    "evidence", "attachments", "impact", "choices", "reply_syntax",
    "freshness", "redaction_attestations",
})


def new_approval_authorizer() -> str:
    """Return the private nonce used only by the durable decision boundary."""
    return secrets.token_urlsafe(12)


def _text(value: Any, limit: int, *, field: str, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"approval {field} must be text")
    # Bound work before the redactor without allowing the discarded tail to
    # enter durable state.  The retained fields are far shorter than this cap.
    raw = "".join(ch for ch in value[:32_000] if ch in "\n\t" or ch.isprintable())
    try:
        from agent.monitoring.redaction import redact_for_export

        clean = redact_for_export(raw)
    except Exception as exc:  # pragma: no cover - redactor itself fails closed
        raise ValueError("approval redaction unavailable") from exc
    clean = " ".join((clean or "[redaction-unavailable]").split())[:limit].strip()
    if required and not clean:
        raise ValueError(f"approval {field} is required")
    return clean


def validate_approval_input(value: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate worker-supplied packet hints before a task is mutated.

    Missing/empty choices intentionally select the conservative fallback.
    Any supplied malformed shape raises ``ValueError`` so ``block_task`` can
    fail closed while the task is still runnable.
    """
    if value is None:
        return {}
    if type(value) is not dict:
        raise ValueError("approval must be an object")
    out = dict(value)
    unknown = set(out).difference(_APPROVAL_INPUT_FIELDS)
    if unknown:
        raise ValueError(f"approval contains unknown fields: {sorted(unknown)}")
    for field in ("decision_question", "completed_state"):
        if field in out and out[field] is not None and not isinstance(out[field], str):
            raise ValueError(f"approval {field} must be text")
    evidence = out.get("evidence_refs")
    if evidence is not None and type(evidence) is not list:
        raise ValueError("approval evidence_refs must be a list")
    if evidence is not None:
        for index, item in enumerate(evidence):
            if isinstance(item, str):
                continue
            if type(item) is not dict:
                raise ValueError(
                    f"approval evidence_refs[{index}] must be text or an object"
                )
            unknown = set(item).difference({"kind", "ref", "label"})
            if unknown:
                raise ValueError(
                    f"approval evidence_refs[{index}] contains unknown fields: "
                    f"{sorted(unknown)}"
                )
    choices = out.get("choices")
    if choices in (None, []):
        return out
    if type(choices) is not list:
        raise ValueError("approval choices must be a list")
    if not 1 <= len(choices) <= 4:
        raise ValueError("approval choices must contain one to four entries")
    expected_ids = list(CHOICE_IDS[: len(choices)])
    normalized: list[dict[str, Any]] = []
    for index, choice in enumerate(choices):
        if type(choice) is not dict:
            raise ValueError(f"approval choice {expected_ids[index]} must be an object")
        unknown = set(choice).difference(
            {"id", "label", "tradeoff", "recommended", "action", "assignee"}
        )
        if unknown:
            raise ValueError(
                f"approval choice {expected_ids[index]} contains unknown fields: "
                f"{sorted(unknown)}"
            )
        choice_id = choice.get("id")
        if choice_id != expected_ids[index]:
            raise ValueError("approval choice ids must be consecutive A/B/C/D")
        recommended = choice.get("recommended")
        if not isinstance(recommended, bool):
            raise ValueError(f"approval choice {choice_id} recommended must be boolean")
        label = choice.get("label")
        tradeoff = choice.get("tradeoff")
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"approval choice {choice_id} label is required")
        if not isinstance(tradeoff, str) or not tradeoff.strip():
            raise ValueError(f"approval choice {choice_id} tradeoff is required")
        action = choice.get("action")
        if action is not None:
            if not isinstance(action, str) or action not in {
                "resume",
                "keep_blocked",
                "decompose",
            }:
                raise ValueError(f"approval choice {choice_id} action is invalid")
        assignee = choice.get("assignee")
        if assignee is not None:
            if action != "resume":
                raise ValueError(
                    f"approval choice {choice_id} assignee requires action=resume"
                )
            if (
                not isinstance(assignee, str)
                or not assignee.strip()
                or len(assignee.strip()) > MAX_ASSIGNEE
                or any(not char.isprintable() for char in assignee)
            ):
                raise ValueError(
                    f"approval choice {choice_id} assignee is malformed"
                )
        normalized.append(dict(choice))
    if sum(bool(choice["recommended"]) for choice in normalized) != 1:
        raise ValueError("approval choices must have exactly one recommendation")
    out["choices"] = normalized
    return out


def _evidence(
    supplied: Any,
    *,
    event_id: int,
    comments: Sequence[Mapping[str, Any]],
    attachments: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if supplied:
        rows = supplied
    else:
        rows = []
        for comment in list(comments)[-3:]:
            rows.append({
                "kind": "task_comment",
                "ref": f"comment:{comment.get('id')}",
                "label": f"Latest task comment by {comment.get('author') or 'unknown'}.",
            })
        for attachment in list(attachments)[-3:]:
            rows.append({
                "kind": "attachment",
                "ref": f"attachment:{attachment.get('id')}",
                "label": f"Attached file: {attachment.get('filename') or 'unnamed'}.",
            })
        if not rows:
            rows = [
                {
                    "kind": "task_event",
                    "ref": f"event:{event_id}",
                    "label": "Blocking event; no external evidence was supplied.",
                }
            ]

    clean: list[dict[str, Any]] = []
    for index, item in enumerate(list(rows)[:MAX_EVIDENCE]):
        if isinstance(item, str):
            item = {"kind": "reference", "ref": item, "label": item}
        if not isinstance(item, Mapping):
            raise ValueError(
                f"approval evidence_refs[{index}] must be text or an object"
            )
        kind = _text(
            item.get("kind") or "reference",
            40,
            field=f"evidence_refs[{index}].kind",
            required=True,
        )
        ref = _text(
            item.get("ref"),
            MAX_EVIDENCE_TEXT,
            field=f"evidence_refs[{index}].ref",
            required=True,
        )
        label = _text(
            item.get("label") or ref,
            MAX_EVIDENCE_TEXT,
            field=f"evidence_refs[{index}].label",
            required=True,
        )
        clean.append({"kind": kind, "ref": ref, "label": label})
    return clean


def _choices(supplied: Any) -> list[dict[str, Any]]:
    if not supplied:
        return [
            {
                "id": "A",
                "label": "Resume using the current context",
                "tradeoff": "Resumes without new facts and may encounter the same blocker again.",
                "recommended": False,
            },
            {
                "id": "B",
                "label": "Keep the task blocked and ask for clarification",
                "tradeoff": "Keeps the task paused while the worker's request is clarified.",
                "recommended": False,
                "resume": False,
            },
            {
                "id": "C",
                "label": "Keep the task blocked",
                "tradeoff": "Makes no task change and leaves dependents waiting.",
                "recommended": True,
                "resume": False,
            },
        ]
    out: list[dict[str, Any]] = []
    for choice in supplied:
        clean_choice = {
            "id": choice["id"],
            "label": _text(
                choice["label"],
                MAX_CHOICE_LABEL,
                field=f"choice {choice['id']} label",
                required=True,
            ),
            "tradeoff": _text(
                choice["tradeoff"],
                MAX_TRADEOFF,
                field=f"choice {choice['id']} tradeoff",
                required=True,
            ),
            "recommended": bool(choice["recommended"]),
        }
        if choice.get("action") is not None:
            clean_choice["action"] = choice["action"]
        if choice.get("assignee") is not None:
            clean_choice["assignee"] = _text(
                choice["assignee"],
                MAX_ASSIGNEE,
                field=f"choice {choice['id']} assignee",
                required=True,
            )
        out.append(clean_choice)
    return out


def build_approval_packet(
    *,
    task_id: str,
    board_slug: str,
    title: str,
    reason: str,
    block_kind: Optional[str],
    event_id: int,
    event_kind: str,
    approval: Optional[Mapping[str, Any]] = None,
    comments: Sequence[Mapping[str, Any]] = (),
    attachments: Sequence[Mapping[str, Any]] = (),
    dependents: Sequence[Mapping[str, Any]] = (),
    now: Optional[int] = None,
    packet_id: Optional[str] = None,
    generation: int = 1,
) -> dict[str, Any]:
    hints = validate_approval_input(approval)
    created_at = int(now if now is not None else time.time())
    packet_id = packet_id or f"appr_{secrets.token_hex(12)}"
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise ValueError("approval generation must be a positive integer")

    clean_attachments = [
        {
            "id": item.get("id"),
            "filename": _text(
                item.get("filename"), 200, field="attachment filename", required=True
            ),
            "content_type": _text(
                item.get("content_type") or "application/octet-stream",
                100,
                field="attachment content_type",
            ),
            "size": max(0, int(item.get("size") or 0)),
        }
        for item in list(attachments)[:MAX_ATTACHMENTS]
    ]
    waiting = [
        {
            "task_id": str(item.get("task_id") or "")[:80],
            "title": _text(
                item.get("title") or "Untitled dependent",
                MAX_TITLE,
                field="dependent title",
            ),
            "status": str(item.get("status") or "")[:40],
        }
        for item in list(dependents)[:MAX_DEPENDENTS]
        if item.get("task_id")
    ]
    clean_reason = _text(reason, MAX_REASON, field="why_blocked", required=True)
    question = hints.get("decision_question") or (
        clean_reason
        if clean_reason.endswith("?")
        else "What input should Hermes use to continue this task?"
    )
    completed_state = hints.get("completed_state") or (
        "No safe-state handoff was supplied; the task remains paused."
    )
    packet = {
        "schema_version": SCHEMA_VERSION,
        "packet_id": str(packet_id),
        "task_id": str(task_id),
        "board_slug": str(board_slug or "default")[:80],
        "title": _text(title, MAX_TITLE, field="title", required=True),
        "decision_question": _text(
            question, MAX_QUESTION, field="decision_question", required=True
        ),
        "why_blocked": clean_reason,
        "block_kind": str(block_kind or "actionable")[:40],
        "completed_state": _text(
            completed_state, MAX_COMPLETED_STATE, field="completed_state", required=True
        ),
        "evidence": _evidence(
            hints.get("evidence_refs"),
            event_id=event_id,
            comments=comments,
            attachments=clean_attachments,
        ),
        "attachments": clean_attachments,
        "impact": {"waiting_count": len(waiting), "dependents": waiting},
        "choices": _choices(hints.get("choices")),
        "reply_syntax": {
            "short": "Reply A/B/C/D",
            "command": f"/kanban decide {task_id} <choice>",
        },
        "freshness": {"created_at": created_at, "generation": generation},
        "redaction_attestations": {
            "bounded": True,
            "pii_redacted": True,
            "secrets_redacted": True,
        },
    }
    encoded = json.dumps(
        packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(encoded.encode("utf-8")) > MAX_PACKET_JSON_BYTES:
        raise ValueError("approval packet exceeds the durable size limit")
    return packet


def parse_approval_packet(raw: Any) -> dict[str, Any]:
    """Strictly parse stored/API packet data; malformed packets fail closed."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed approval packet JSON") from exc
    if type(value) is not dict or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported approval packet schema")
    required = (
        "packet_id",
        "task_id",
        "board_slug",
        "title",
        "decision_question",
        "why_blocked",
        "block_kind",
        "completed_state",
        "evidence",
        "attachments",
        "impact",
        "choices",
        "reply_syntax",
        "freshness",
        "redaction_attestations",
    )
    if any(key not in value for key in required):
        raise ValueError("approval packet is missing required fields")
    unknown = set(value).difference(_PACKET_FIELDS)
    if unknown:
        raise ValueError(f"approval packet contains unknown fields: {sorted(unknown)}")

    def bounded_text(
        field: str, limit: int, *, source: Mapping[str, Any] = value
    ) -> str:
        item = source.get(field)
        if (
            not isinstance(item, str)
            or not item
            or len(item) > limit
            or any(not char.isprintable() for char in item)
        ):
            raise ValueError(f"approval packet {field} is malformed")
        return item

    bounded_text("packet_id", 160)
    task_id = bounded_text("task_id", 80)
    bounded_text("board_slug", 80)
    bounded_text("title", MAX_TITLE)
    bounded_text("decision_question", MAX_QUESTION)
    bounded_text("why_blocked", MAX_REASON)
    bounded_text("block_kind", 40)
    bounded_text("completed_state", MAX_COMPLETED_STATE)

    choices = value.get("choices")
    if type(choices) is not list or not 1 <= len(choices) <= 4:
        raise ValueError("approval packet choices are malformed")
    if any(type(choice) is not dict for choice in choices):
        raise ValueError("approval packet choice is malformed")
    for choice in choices:
        unknown = set(choice).difference(
            {
                "id",
                "label",
                "tradeoff",
                "recommended",
                "resume",
                "action",
                "assignee",
            }
        )
        if unknown:
            raise ValueError(f"approval packet choice contains unknown fields: {sorted(unknown)}")
    validate_approval_input({
        "choices": [
            {key: item for key, item in choice.items() if key != "resume"}
            for choice in choices
        ]
    })
    for choice in choices:
        if len(choice["label"]) > MAX_CHOICE_LABEL:
            raise ValueError("approval packet choice label is malformed")
        if len(choice["tradeoff"]) > MAX_TRADEOFF:
            raise ValueError("approval packet choice tradeoff is malformed")
        if "resume" in choice and not isinstance(choice["resume"], bool):
            raise ValueError("approval packet choice resume flag is malformed")
        if "resume" in choice and "action" in choice:
            raise ValueError("approval packet choice action is ambiguous")
        if "assignee" in choice:
            assignee = choice["assignee"]
            if (
                not isinstance(assignee, str)
                or not assignee.strip()
                or len(assignee) > MAX_ASSIGNEE
                or any(not char.isprintable() for char in assignee)
            ):
                raise ValueError("approval packet choice assignee is malformed")

    evidence = value.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_EVIDENCE:
        raise ValueError("approval packet evidence is malformed")
    for item in evidence:
        if not isinstance(item, Mapping):
            raise ValueError("approval packet evidence is malformed")
        unknown = set(item).difference({"kind", "ref", "label"})
        if unknown:
            raise ValueError(f"approval packet evidence contains unknown fields: {sorted(unknown)}")
        bounded_text("kind", 40, source=item)
        bounded_text("ref", MAX_EVIDENCE_TEXT, source=item)
        bounded_text("label", MAX_EVIDENCE_TEXT, source=item)

    attachments = value.get("attachments")
    if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS:
        raise ValueError("approval packet attachments are malformed")
    for item in attachments:
        if not isinstance(item, Mapping):
            raise ValueError("approval packet attachments are malformed")
        unknown = set(item).difference({"id", "filename", "content_type", "size"})
        if unknown:
            raise ValueError(f"approval packet attachment contains unknown fields: {sorted(unknown)}")
        bounded_text("filename", 200, source=item)
        bounded_text("content_type", 100, source=item)
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("approval packet attachment size is malformed")

    impact = value.get("impact")
    if not isinstance(impact, Mapping):
        raise ValueError("approval packet impact is malformed")
    if set(impact).difference({"waiting_count", "dependents"}):
        raise ValueError("approval packet impact contains unknown fields")
    dependents = impact.get("dependents")
    waiting_count = impact.get("waiting_count")
    if (
        isinstance(waiting_count, bool)
        or not isinstance(waiting_count, int)
        or waiting_count < 0
        or not isinstance(dependents, list)
        or len(dependents) > MAX_DEPENDENTS
        or waiting_count != len(dependents)
    ):
        raise ValueError("approval packet impact is malformed")
    for item in dependents:
        if not isinstance(item, Mapping):
            raise ValueError("approval packet dependent is malformed")
        if set(item).difference({"task_id", "title", "status"}):
            raise ValueError("approval packet dependent contains unknown fields")
        bounded_text("task_id", 80, source=item)
        bounded_text("title", MAX_TITLE, source=item)
        bounded_text("status", 40, source=item)

    reply = value.get("reply_syntax")
    if not isinstance(reply, Mapping):
        raise ValueError("approval packet reply syntax is malformed")
    if set(reply) != {"short", "command"}:
        raise ValueError("approval packet reply syntax contains unknown fields")
    if reply.get("short") != "Reply A/B/C/D" or reply.get("command") != (
        f"/kanban decide {task_id} <choice>"
    ):
        raise ValueError("approval packet reply syntax is malformed")

    freshness = value.get("freshness")
    if not isinstance(freshness, Mapping):
        raise ValueError("approval packet freshness is malformed")
    if set(freshness) != {"created_at", "generation"}:
        raise ValueError("approval packet freshness contains unknown fields")
    created_at = freshness.get("created_at")
    generation = freshness.get("generation")
    if (
        isinstance(created_at, bool)
        or not isinstance(created_at, int)
        or created_at < 0
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise ValueError("approval packet freshness is malformed")

    if value.get("redaction_attestations") != {
        "bounded": True,
        "pii_redacted": True,
        "secrets_redacted": True,
    }:
        raise ValueError("approval packet redaction attestations are malformed")

    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("approval packet is not JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_PACKET_JSON_BYTES:
        raise ValueError("approval packet exceeds the durable size limit")
    return dict(value)


def format_approval_packet_text(
    packet: Mapping[str, Any], *, allow_short_reply: bool = False
) -> str:
    """Readable text fallback retained on every delivery path.

    Bare ``A/B/C/D`` interception is Telegram-specific.  Other surfaces get
    only the explicit slash command so a letter cannot disappear into their
    normal chat path while appearing to have applied a decision.
    """
    canonical = dict(packet)
    for annotation in ("provenance", "delivery", "deliveries"):
        canonical.pop(annotation, None)
    parsed = parse_approval_packet(canonical)
    choice_lines = []
    for choice in parsed["choices"]:
        marker = " (recommended)" if choice["recommended"] else ""
        choice_lines.append(
            f"{choice['id']}. {choice['label']}{marker} — {choice['tradeoff']}"
        )
    waiting = int((parsed.get("impact") or {}).get("waiting_count") or 0)
    evidence_refs = ", ".join(item["ref"] for item in parsed["evidence"][:4])
    reply = parsed["reply_syntax"]["command"]
    if allow_short_reply:
        reply = f"A/B/C/D or {reply}"
    return "\n".join([
        f"⚠ Approval required · [{parsed['board_slug']}] {parsed['task_id']}",
        parsed["title"],
        f"Decision: {parsed['decision_question']}",
        f"Why blocked: {parsed['why_blocked']}",
        f"Safe state: {parsed['completed_state']}",
        f"Evidence: {evidence_refs}",
        f"Impact: {waiting} dependent task(s) waiting",
        *choice_lines,
        f"Reply {reply}",
        f"Freshness: {parsed['freshness']['created_at']} · generation {parsed['freshness']['generation']}",
    ])


def render_approval_card(packet: Mapping[str, Any], output_path) -> str:
    """Render a deterministic PNG card using Pillow's bundled default font.

    No font lookup or network access occurs.  The text fallback remains the
    authoritative content; this artifact is an additional readable surface.
    """
    from pathlib import Path

    from PIL import Image, ImageDraw, ImageFont

    parsed = parse_approval_packet(packet)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        title_font = ImageFont.load_default(size=38)
        heading_font = ImageFont.load_default(size=25)
        body_font = ImageFont.load_default(size=22)
        small_font = ImageFont.load_default(size=18)
    except TypeError:  # pragma: no cover - compatibility with old Pillow
        title_font = heading_font = body_font = small_font = ImageFont.load_default()

    width = 1200
    content_left = 78
    content_right = width - 78
    content_width = content_right - content_left
    measure_image = Image.new("RGB", (1, 1), "#0b1020")
    measure = ImageDraw.Draw(measure_image)

    def text_box(text: str, font) -> tuple[int, int, int, int]:
        return measure.textbbox((0, 0), text, font=font)

    def text_width(text: str, font) -> int:
        left, _, right, _ = text_box(text, font)
        return right - left

    def split_long_token(token: str, font, max_width: int) -> list[str]:
        """Split one token at measured character boundaries."""
        chunks: list[str] = []
        remaining = token
        while remaining:
            low, high = 1, len(remaining)
            fit = 0
            while low <= high:
                midpoint = (low + high) // 2
                if text_width(remaining[:midpoint], font) <= max_width:
                    fit = midpoint
                    low = midpoint + 1
                else:
                    high = midpoint - 1
            # The content column is much wider than every bundled-font glyph,
            # but retaining one character makes this helper fail-safe if that
            # invariant changes.
            fit = max(1, fit)
            chunks.append(remaining[:fit])
            remaining = remaining[fit:]
        return chunks

    def wrap_pixels(text: str, font, max_width: int) -> list[str]:
        """Wrap text by Pillow metrics, breaking over-wide bare tokens."""
        words = str(text).split()
        if not words:
            return [""]
        wrapped: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}" if current else word
            if text_width(candidate, font) <= max_width:
                current = candidate
                continue
            if current:
                wrapped.append(current)
                current = ""
            chunks = split_long_token(word, font, max_width)
            wrapped.extend(chunks[:-1])
            current = chunks[-1]
        if current:
            wrapped.append(current)
        return wrapped

    def positioned_lines(
        text_lines: Sequence[str],
        font,
        start_y: int,
        *,
        gap: int,
    ) -> tuple[list[tuple[str, int]], int]:
        """Return exact draw origins and the measured bottom edge."""
        positions: list[tuple[str, int]] = []
        y = start_y
        bottom = start_y
        for line in text_lines:
            positions.append((line, y))
            bottom = y + text_box(line, font)[3]
            y = bottom + gap
        return positions, bottom

    def left_origin(text: str, font, left: int = content_left) -> int:
        return left - text_box(text, font)[0]

    def right_origin(text: str, font, right: int = content_right) -> int:
        return right - text_box(text, font)[2]

    header_label = "APPROVAL REQUIRED"
    header_identity = f"{parsed['board_slug']} · {parsed['task_id']}"
    header_label_width = text_width(header_label, heading_font)
    header_identity_left = content_left + header_label_width + 32
    header_identity_lines = wrap_pixels(
        header_identity,
        small_font,
        content_right - header_identity_left,
    )
    header_label_y = 62
    header_positions, header_identity_bottom = positioned_lines(
        header_identity_lines,
        small_font,
        58,
        gap=3,
    )
    header_label_bottom = header_label_y + text_box(header_label, heading_font)[3]
    header_bottom = max(header_label_bottom, header_identity_bottom) + 24

    title_lines = wrap_pixels(parsed["title"], title_font, content_width)
    section_values = (
        ("Decision", parsed["decision_question"], "#fff3c4"),
        ("Why blocked", parsed["why_blocked"], "#d8e2ff"),
        ("Safe state", parsed["completed_state"], "#d8e2ff"),
    )
    choice_text_left = 142
    choice_text_width = content_right - choice_text_left
    choice_line_groups = [
        wrap_pixels(
            f"{choice['label']} - {choice['tradeoff']}",
            small_font,
            choice_text_width,
        )
        for choice in parsed["choices"]
    ]
    evidence = ", ".join(item["ref"] for item in parsed["evidence"][:4])
    evidence_lines = wrap_pixels(evidence, body_font, content_width)

    # Build the vertical layout from the same measured text boxes later drawn.
    # Padding is intentional; line and block heights are not approximations.
    y = header_bottom + 36
    title_positions, title_bottom = positioned_lines(
        title_lines, title_font, y, gap=8
    )
    y = title_bottom + 24
    section_layouts = []
    for label, value, color in section_values:
        label_y = y
        label_bottom = label_y + text_box(label.upper(), small_font)[3]
        value_lines = wrap_pixels(value, body_font, content_width)
        value_positions, value_bottom = positioned_lines(
            value_lines,
            body_font,
            label_bottom + 8,
            gap=5,
        )
        section_layouts.append((label, color, label_y, value_positions))
        y = value_bottom + 24

    choices_label_y = y
    y = choices_label_y + text_box("CHOICES", small_font)[3] + 14
    choice_layouts = []
    for choice, wrapped in zip(parsed["choices"], choice_line_groups):
        box_top = y
        heading_y = box_top + 16
        choice_id_bottom = heading_y + text_box(choice["id"], heading_font)[3]
        recommended = bool(choice["recommended"])
        recommended_bottom = (
            heading_y + text_box("RECOMMENDED", small_font)[3]
            if recommended
            else heading_y
        )
        line_positions, text_bottom = positioned_lines(
            wrapped,
            small_font,
            max(choice_id_bottom, recommended_bottom) + 10,
            gap=4,
        )
        box_bottom = text_bottom + 18
        choice_layouts.append(
            (choice, box_top, box_bottom, heading_y, line_positions)
        )
        y = box_bottom + 16

    evidence_label_y = y
    evidence_label_bottom = (
        evidence_label_y + text_box("EVIDENCE", small_font)[3]
    )
    evidence_positions, evidence_bottom = positioned_lines(
        evidence_lines,
        body_font,
        evidence_label_bottom + 8,
        gap=5,
    )
    content_bottom = evidence_bottom

    waiting = int((parsed.get("impact") or {}).get("waiting_count") or 0)
    impact_text = f"Impact: {waiting} dependent task(s) waiting"
    reply_text = f"Reply A/B/C/D · {parsed['reply_syntax']['command']}"
    freshness_text = (
        f"Freshness {parsed['freshness']['created_at']} · "
        f"generation {parsed['freshness']['generation']}"
    )
    reply_lines = wrap_pixels(reply_text, body_font, content_width)
    footer_impact_y = 18
    footer_impact_bottom = footer_impact_y + text_box(impact_text, small_font)[3]
    footer_reply_positions, footer_reply_bottom = positioned_lines(
        reply_lines,
        body_font,
        footer_impact_bottom + 9,
        gap=4,
    )
    footer_freshness_y = footer_reply_bottom + 10
    footer_height = footer_freshness_y + text_box(freshness_text, small_font)[3]
    height = max(1500, content_bottom + 24 + footer_height + 40)
    if height > 4096:
        raise ValueError("approval card exceeds the 4096px height limit")
    footer_top = height - 40 - footer_height

    image = Image.new("RGB", (width, height), "#0b1020")
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle(
        (44, 40, width - 44, height - 40),
        radius=28,
        fill="#121a2f",
        outline="#39476a",
        width=3,
    )
    draw.rounded_rectangle(
        (44, 40, width - 44, header_bottom),
        radius=28,
        fill="#f59e0b",
    )
    draw.text(
        (left_origin(header_label, heading_font), header_label_y),
        header_label,
        font=heading_font,
        fill="#17100a",
    )
    for line, line_y in header_positions:
        draw.text(
            (right_origin(line, small_font), line_y),
            line,
            font=small_font,
            fill="#3b2708",
        )

    for line, line_y in title_positions:
        draw.text(
            (left_origin(line, title_font), line_y),
            line,
            font=title_font,
            fill="#ffffff",
        )
    for label, color, label_y, value_positions in section_layouts:
        upper_label = label.upper()
        draw.text(
            (left_origin(upper_label, small_font), label_y),
            upper_label,
            font=small_font,
            fill="#8ea2d0",
        )
        for line, line_y in value_positions:
            draw.text(
                (left_origin(line, body_font), line_y),
                line,
                font=body_font,
                fill=color,
            )

    draw.text(
        (left_origin("CHOICES", small_font), choices_label_y),
        "CHOICES",
        font=small_font,
        fill="#8ea2d0",
    )
    for choice, box_top, box_bottom, heading_y, line_positions in choice_layouts:
        recommended = bool(choice["recommended"])
        fill = "#1f513c" if recommended else "#1a2440"
        outline = "#4ade80" if recommended else "#39476a"
        # Pillow's bundled bitmap font is intentionally used to avoid network
        # or host-font dependencies; keep punctuation in its ASCII repertoire.
        draw.rounded_rectangle(
            (72, box_top, width - 72, box_bottom),
            radius=16,
            fill=fill,
            outline=outline,
            width=2,
        )
        draw.text(
            (left_origin(choice["id"], heading_font, 92), heading_y),
            choice["id"],
            font=heading_font,
            fill="#ffffff",
        )
        label = "RECOMMENDED" if recommended else ""
        if label:
            draw.text(
                (right_origin(label, small_font), heading_y),
                label,
                font=small_font,
                fill="#86efac",
            )
        for line, line_y in line_positions:
            draw.text(
                (left_origin(line, small_font, choice_text_left), line_y),
                line,
                font=small_font,
                fill="#e7ecff",
            )

    draw.text(
        (left_origin("EVIDENCE", small_font), evidence_label_y),
        "EVIDENCE",
        font=small_font,
        fill="#8ea2d0",
    )
    for line, line_y in evidence_positions:
        draw.text(
            (left_origin(line, body_font), line_y),
            line,
            font=body_font,
            fill="#d8e2ff",
        )

    draw.line(
        (content_left, footer_top, content_right, footer_top),
        fill="#39476a",
        width=2,
    )
    draw.text(
        (left_origin(impact_text, small_font), footer_top + footer_impact_y),
        impact_text,
        font=small_font,
        fill="#aebce0",
    )
    for line, line_y in footer_reply_positions:
        draw.text(
            (left_origin(line, body_font), footer_top + line_y),
            line,
            font=body_font,
            fill="#fff3c4",
        )
    draw.text(
        (
            left_origin(freshness_text, small_font),
            footer_top + footer_freshness_y,
        ),
        freshness_text,
        font=small_font,
        fill="#8ea2d0",
    )
    image.save(path, format="PNG", optimize=False, compress_level=6)
    return str(path)


__all__ = [
    "SCHEMA_VERSION",
    "build_approval_packet",
    "format_approval_packet_text",
    "new_approval_authorizer",
    "parse_approval_packet",
    "render_approval_card",
    "validate_approval_input",
]
