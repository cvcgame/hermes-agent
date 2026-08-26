import { cleanup, render, screen } from '@testing-library/react'
import type { ComponentType } from 'react'
import { afterEach, describe, expect, expectTypeOf, it } from 'vitest'

import * as DrawerModule from './drawer'
import type { ApprovalPacket } from './types'

afterEach(cleanup)

const mutationAuthorizer = 'mutation-authorizer-do-not-expose'

const packet: ApprovalPacket = {
  schema_version: 'approval_packet.v1',
  packet_id: 'ap_fixture',
  task_id: 't_abc12345',
  board_slug: 'main',
  title: 'Release the dashboard',
  decision_question: 'Which rollout should we use?',
  why_blocked: 'A human must select the rollout risk.',
  block_kind: 'needs_input',
  completed_state: 'Build and focused tests are complete; task remains blocked.',
  evidence: [{ ref: 'attachment:7', kind: 'attachment', label: 'rollout-report.txt' }],
  attachments: [{ id: 7, filename: 'rollout-report.txt', content_type: 'text/plain', size: 240 }],
  impact: { waiting_count: 1, dependents: [{ task_id: 't_child', title: 'Publish release', status: 'todo' }] },
  choices: [
    { id: 'A', label: 'Staged rollout', tradeoff: 'Slower, lower risk', recommended: true },
    { id: 'B', label: 'Immediate rollout', tradeoff: 'Faster, higher risk', recommended: false }
  ],
  reply_syntax: { short: 'A/B/C/D', command: '/kanban decide t_abc12345 <choice>' },
  freshness: { created_at: 1_777_000_000, generation: 1 },
  redaction_attestations: { bounded: true, pii_redacted: true, secrets_redacted: true },
  provenance: { event_id: 9, event_kind: 'blocked', status: 'open', decision: null },
  deliveries: []
}

describe('Approval required panel', () => {
  it('renders the structured API packet as an actionable, sanitized panel', () => {
    expectTypeOf<ApprovalPacket['freshness']>().toEqualTypeOf<{
      created_at: number
      generation: number
    }>()
    expect(JSON.stringify(packet)).not.toContain(mutationAuthorizer)

    const Panel = (
      DrawerModule as unknown as {
        ApprovalRequiredPanel?: ComponentType<{ packet: ApprovalPacket }>
      }
    ).ApprovalRequiredPanel

    expect(Panel).toBeTypeOf('function')

    if (!Panel) {
      throw new Error('ApprovalRequiredPanel is not implemented')
    }

    const { container } = render(<Panel packet={packet} />)

    expect(screen.getByRole('region', { name: 'Approval required' })).toBeTruthy()
    expect(screen.getByText('Which rollout should we use?')).toBeTruthy()
    expect(screen.getByText(/Build and focused tests are complete/)).toBeTruthy()
    expect(screen.getByText(/Staged rollout/)).toBeTruthy()
    expect(screen.getByText('Recommended')).toBeTruthy()
    expect(screen.getByText(/attachment:7/)).toBeTruthy()
    expect(screen.getByText(/1 dependent task waiting/)).toBeTruthy()
    expect(screen.getByText(/\/kanban decide t_abc12345/)).toBeTruthy()
    expect(screen.getByText('Freshness: 1777000000 · generation 1')).toBeTruthy()
    expect(screen.getByText(/Sanitized/)).toBeTruthy()
    expect(container.textContent).not.toContain(mutationAuthorizer)
    expect(container.textContent).not.toContain('undefined')
    expect(screen.getByRole('region', { name: 'Approval required' }).className).not.toMatch(
      /(?:amber|white|black|rounded-lg)/
    )

    for (const choice of screen.getAllByRole('listitem')) {
      expect(choice.className).not.toMatch(/(?:bg-|rounded)/)
    }

    expect(container.firstChild).toMatchSnapshot()
  })
})
