# Approval Packet v1 storage migration

Approval Packet v1 adds three tables to each Kanban board database:

- `approval_packets` stores one bounded, sanitized packet for each actionable
  block event and its decision provenance. Public packet freshness is exactly
  the creation timestamp plus a per-task generation number starting at 1. The
  existing `freshness_token` column is an internal mutation authorizer and is
  never serialized into the sanitized packet.
- `approval_deliveries` stores idempotent per-destination text/media delivery
  and readback state. Delivery or readback does not mean a decision was made.
- `approval_decision_audit` stores accepted and rejected explicit decision
  attempts. Only bounded authorizer digests are stored in the audit log.

The normal `hermes_cli.kanban_db` initialization seam applies the migration.
All table and index statements use `CREATE ... IF NOT EXISTS`, so opening the
same database repeatedly or concurrently is idempotent. Existing task, event,
subscription, comment, attachment, and dependency rows are unchanged.

Approval card PNG files are additive artifacts under the board database's
`approval_cards/` sibling directory. Text remains the required delivery path;
rendering or media-delivery failure does not remove that fallback.
Only a card whose resolved path remains inside that board-owned directory is
eligible for media delivery. Permanent task deletion removes the packet,
delivery, audit rows, and matching generated cards together.

Telegram decisions are bound to the task subscription's platform, chat,
thread, and stable subscriber identity. Bare `A/B/C/D` is Telegram-only; other
surfaces advertise the explicit `/kanban decide <task-id> <choice>` form.
Replies to a delivered text or media message bind to that exact delivery. A
standalone decision is accepted only when one delivered packet generation is
unambiguous on the route/task. Leaving the blocked/triage decision state
supersedes the packet, so it cannot unlock a later unrelated blocker.

Choice actions are bounded and explicit. A missing action keeps the v1
ordinary-resume behavior; `resume` may also reassign the existing root before
its parent-aware promotion, `keep_blocked` leaves it paused, and `decompose`
records a `decomposition_requested` event bound to the exact packet generation.
Merely entering `triage` is not decomposition consent. The gateway's automatic
decomposer/specifier only selects tasks with that current explicit intent and
revalidates it inside the final write transaction, so a stale candidate cannot
fan out after a newer or ordinary owner decision. Explicit human
`hermes kanban decompose ...` commands retain their manual behavior. Retrying
the same exact packet and choice is idempotent and does not add another audit
row or repeat task mutation.

## Rollback

Before rolling back, stop processes that write the affected board and back up
both the SQLite database and its `approval_cards/` directory. An older Hermes
binary ignores the additive tables, so dropping them is not required for a
binary rollback. No follow-up schema migration is needed for public
generations: the internal `freshness_token` column is intentionally retained,
while `generation`, bounded choice actions, and decomposition intent references
live in sanitized packet JSON and existing task events. Before running an older
binary, resolve or supersede any open packet that contains a choice action: an
older strict v1 parser correctly rejects that unknown field rather than
silently applying a different action.

If the new approval history must be removed after a backup, drop
`approval_decision_audit`, then `approval_deliveries`, then `approval_packets`.
That operation permanently removes approval delivery/readback and decision
audit history but does not revert task state or delete task comments/events.
Remove `approval_cards/` separately only if those generated media artifacts
are also no longer required. Reopening the database with a version that
contains Approval Packet v1 recreates the empty tables idempotently.
