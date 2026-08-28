"""Deterministic, local-font-only Approval Packet card rendering."""

from __future__ import annotations

from PIL import Image, ImageDraw

from hermes_cli import approval_packets


def _fixture_packet() -> dict:
    return approval_packets.build_approval_packet(
        task_id="t_abcd1234",
        board_slug="release",
        title="Choose a release strategy",
        reason="The worker needs an operator decision before deployment.",
        block_kind="needs_input",
        event_id=42,
        event_kind="blocked",
        approval={
            "decision_question": "Deploy now or wait for the staffed window?",
            "completed_state": "Build and tests are complete; deployment has not started.",
            "evidence_refs": [
                {"kind": "test", "ref": "report:42", "label": "Release report"}
            ],
            "choices": [
                {
                    "id": "A",
                    "label": "Wait for the window",
                    "tradeoff": "Slower; staffed rollback support is available.",
                    "recommended": True,
                },
                {
                    "id": "B",
                    "label": "Deploy now",
                    "tradeoff": "Faster; on-call risk is higher.",
                    "recommended": False,
                },
            ],
        },
        dependents=[
            {"task_id": "t_dep", "title": "Publish release notes", "status": "todo"}
        ],
        now=1_700_000_000,
        packet_id="appr_fixture",
        generation=1,
    )


def test_approval_card_is_a_deterministic_png(tmp_path):
    packet = _fixture_packet()
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"

    approval_packets.render_approval_card(packet, first)
    approval_packets.render_approval_card(packet, second)

    assert first.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert first.read_bytes() == second.read_bytes()
    assert 10_000 < first.stat().st_size < 1_000_000


def test_generic_packet_text_does_not_advertise_telegram_only_short_replies():
    packet = _fixture_packet()

    text = approval_packets.format_approval_packet_text(packet)

    assert "Reply A/B/C/D" not in text
    assert packet["reply_syntax"]["command"] in text


def test_approval_card_maximum_bounded_text_fits_inner_bounds(
    tmp_path,
    monkeypatch,
):
    packet = approval_packets.build_approval_packet(
        task_id="t_" + "a" * 64,
        board_slug="release",
        title="T" * approval_packets.MAX_TITLE,
        reason="R" * approval_packets.MAX_REASON,
        block_kind="needs_input",
        event_id=99,
        event_kind="blocked",
        approval={
            "decision_question": "Q" * approval_packets.MAX_QUESTION,
            "completed_state": "S" * approval_packets.MAX_COMPLETED_STATE,
            "evidence_refs": [
                {
                    "kind": "report",
                    "ref": f"report:{index}:" + "e" * 180,
                    "label": "Evidence " + "l" * 180,
                }
                for index in range(4)
            ],
            "choices": [
                {
                    "id": choice_id,
                    "label": "L" * approval_packets.MAX_CHOICE_LABEL,
                    "tradeoff": "X" * approval_packets.MAX_TRADEOFF,
                    "recommended": index == 0,
                }
                for index, choice_id in enumerate("ABCD")
            ],
        },
        now=1_700_000_000,
        packet_id="appr_max_fixture",
        generation=1,
    )
    output = tmp_path / "max.png"

    drawn_text = []
    pillow_draw = ImageDraw.Draw

    class BoundsRecordingDraw:
        def __init__(self, *args, **kwargs):
            self._draw = pillow_draw(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._draw, name)

        def text(self, xy, text, *args, **kwargs):
            font = kwargs.get("font")
            drawn_text.append({
                "text": text,
                "bbox": self._draw.textbbox(xy, text, font=font),
            })
            return self._draw.text(xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw, "Draw", BoundsRecordingDraw)

    approval_packets.render_approval_card(packet, output)

    with Image.open(output) as image:
        assert image.height > 1500
        assert image.height <= 4096
        inner_bounds = (78, 40, image.width - 78, image.height - 40)

    header_identity = f"{packet['board_slug']} · {packet['task_id']}"
    footer_reply = f"Reply A/B/C/D · {packet['reply_syntax']['command']}"
    freshness = (
        f"Freshness {packet['freshness']['created_at']} · "
        f"generation {packet['freshness']['generation']}"
    )
    static_text = {
        "APPROVAL REQUIRED",
        "DECISION",
        "WHY BLOCKED",
        "SAFE STATE",
        "CHOICES",
        "EVIDENCE",
        "RECOMMENDED",
        *tuple("ABCD"),
    }
    variable_text = [item for item in drawn_text if item["text"] not in static_text]

    def compact(value):
        return "".join(str(value).split())

    all_variable_text = compact("".join(item["text"] for item in variable_text))
    assert compact(header_identity) in all_variable_text
    assert compact(footer_reply) in all_variable_text
    assert compact(freshness) in all_variable_text
    for value in (
        packet["title"],
        packet["decision_question"],
        packet["why_blocked"],
        packet["completed_state"],
    ):
        assert compact(value) in all_variable_text

    violations = []
    left, top, right, bottom = inner_bounds
    for item in variable_text:
        x0, y0, x1, y1 = item["bbox"]
        if x0 < left or y0 < top or x1 > right or y1 > bottom:
            violations.append(
                f"text {item['text'][:24]!r} bbox {item['bbox']} "
                f"exceeds {inner_bounds}"
            )

    footer_items = [
        item
        for item in variable_text
        if item["bbox"][1] >= image.height - 150
    ]
    footer_text = compact("".join(item["text"] for item in footer_items))
    assert compact(packet["reply_syntax"]["command"]) in footer_text
    assert compact(freshness) in footer_text
    reply_boxes = [item["bbox"] for item in footer_items if "Freshness" not in item["text"]]
    freshness_boxes = [item["bbox"] for item in footer_items if "Freshness" in item["text"]]
    if reply_boxes and freshness_boxes and max(box[3] for box in reply_boxes) >= min(
        box[1] for box in freshness_boxes
    ):
        violations.append(
            "footer reply command overlaps freshness line: "
            f"{reply_boxes} vs {freshness_boxes}"
        )

    assert not violations, "\n".join(violations)
