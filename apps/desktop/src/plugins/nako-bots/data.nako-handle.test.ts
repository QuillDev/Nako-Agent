/**
 * @nako belongs to the default bot only while its source has no real
 * (pre-reservation) `nako` profile. With one, the roster shows exactly one
 * @nako — the real profile — and the default answers to @default, so a
 * typed @nako reaches the same profile the backend's message_agent does.
 */

import { describe, expect, it, vi } from 'vitest'

import { botHandle, botMentionTag, claimDefaultHandles, mergeMultiSourceRoster, resolveRosterMentions } from './data'
import { previewKind } from './row-helpers'
import type { RosterRow } from './types'

vi.mock('@nako/plugin-sdk', async () => {
  const { atom } = await import('nanostores')

  return { atom, host: {}, queryClient: undefined, useQuery: vi.fn(), useValue: vi.fn() }
})

vi.mock('./shared', () => ({ getPluginCtx: () => null, ID: 'nako-bots' }))

function roster(names: string[]): RosterRow[] {
  const local = { profiles: names.map(name => ({ name })) }

  // What Electron's buildAgentRoster reports for a single local source: bare-name handles.
  const union = {
    agents: names.map(profile => ({
      connectionId: 'local',
      connectionKind: 'local' as const,
      connectionLabel: 'This device',
      handle: profile,
      profile,
      targetProfile: profile
    })),
    primaryConnectionId: 'local'
  }

  return mergeMultiSourceRoster(local, union, 'local').profiles as RosterRow[]
}

describe('the @nako handle', () => {
  it.each([
    [['default', 'researcher'], 'default'],
    [['default', 'nako', 'researcher'], 'nako']
  ])('roster %j shows one @nako and a typed @nako reaches %s', (names, owner) => {
    const rows = roster(names)
    const handles = rows.map(row => botHandle(row.name, row))

    expect(handles.filter(handle => handle === 'nako')).toHaveLength(1)
    expect(rows.map(row => botMentionTag(row)).filter(tag => tag === 'nako')).toHaveLength(1)
    expect(resolveRosterMentions('@nako ping', rows, { name: 'researcher' }).map(row => row.name)).toEqual([owner])
  })

  it('names the default @default once a real nako profile holds the handle', () => {
    const rows = roster(['default', 'nako', 'researcher'])
    const byName = Object.fromEntries(rows.map(row => [row.name, botHandle(row.name, row)]))

    expect(byName).toEqual({ default: 'default', nako: 'nako', researcher: 'researcher' })
    expect(resolveRosterMentions('@default ping', rows, { name: 'researcher' }).map(row => row.name)).toEqual(['default'])
  })

  it('keeps each source separate: a remote nako profile does not rename the local default', () => {
    const rows = claimDefaultHandles<Partial<RosterRow>>([
      { connectionId: 'local', name: 'default' },
      { connectionId: 'mini', name: 'default' },
      { connectionId: 'mini', name: 'nako' }
    ])

    expect(rows.map(row => botHandle(String(row.name), row))).toEqual(['nako', 'default', 'nako'])
  })

  it("never lets a source's stale 'nako' handle on its default answer @nako", () => {
    // An older backend labels its default @nako even beside a real nako profile.
    const rows = claimDefaultHandles<RosterRow>([
      { connectionId: 'mini', handle: 'nako', name: 'default', remoteSource: true },
      { connectionId: 'mini', handle: 'nako', name: 'nako', remoteSource: true },
      { connectionId: 'local', name: 'researcher' }
    ])

    expect(resolveRosterMentions('@nako ping', rows, { name: 'researcher' }).map(row => row.name)).toEqual(['nako'])
  })

  it('names a DM sender in a row preview by its roster handle', () => {
    const rows = claimDefaultHandles<RosterRow>(
      ['default', 'nako', 'researcher'].map(name => ({ connectionId: 'local', name }))
    )

    const sender = (name: string, list: RosterRow[]) =>
      previewKind(`Message from 🤖 ${name} (@x): hi`, list, 'local').fromBot

    expect([sender('default', rows), sender('nako', rows)]).toEqual(['default', 'nako'])
    // No real nako profile: the default is still @nako (#89484).
    expect(sender('default', claimDefaultHandles<RosterRow>([{ connectionId: 'local', name: 'default' }]))).toBe(
      'nako'
    )
  })
})
