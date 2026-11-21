import { describe, expect, it } from 'vitest'

import {
  normalizeNakoOpenString,
  pathFromNakoDeepLink,
  pathFromOpenDeepLink,
  resolveNakoOpenPath
} from './nako-open-target'

describe('normalizeNakoOpenString', () => {
  it('accepts hash-router paths and strips a leading hash', () => {
    expect(normalizeNakoOpenString('/index-network/intent/1')).toBe('/index-network/intent/1')
    expect(normalizeNakoOpenString('#/index-network/intent/1')).toBe('/index-network/intent/1')
  })

  it('maps plugin-scoped nako:// deep links to the same path', () => {
    expect(normalizeNakoOpenString('nako://index-network/intent/1')).toBe('/index-network/intent/1')
    expect(normalizeNakoOpenString('nako://index-network/intent/1?focus=true')).toBe(
      '/index-network/intent/1?focus=true'
    )
  })

  it('maps nako://open/… deep links by stripping the open host', () => {
    expect(normalizeNakoOpenString('nako://open/index-network/intent/1')).toBe('/index-network/intent/1')
    expect(normalizeNakoOpenString('nako://open/settings/plugins')).toBe('/settings/plugins')
  })

  it('rejects reserved nako kinds and unsafe paths', () => {
    expect(normalizeNakoOpenString('nako://blueprint/morning-brief')).toBeNull()
    expect(normalizeNakoOpenString('nako://plugin/install')).toBeNull()
    expect(normalizeNakoOpenString('https://example.com/x')).toBeNull()
    expect(normalizeNakoOpenString('/../etc/passwd')).toBeNull()
    expect(normalizeNakoOpenString('index-network')).toBeNull()
  })
})

describe('resolveNakoOpenPath', () => {
  it('merges structured path + params', () => {
    expect(resolveNakoOpenPath({ path: '/index-network/intent/1', params: { focus: 'true' } })).toBe(
      '/index-network/intent/1?focus=true'
    )
  })

  it('resolves href the same as a bare string', () => {
    expect(resolveNakoOpenPath({ href: 'nako://index-network/intent/1' })).toBe('/index-network/intent/1')
  })
})

describe('pathFromNakoDeepLink', () => {
  it('builds the navigate path from a plugin-scoped deep-link payload', () => {
    expect(pathFromNakoDeepLink('index-network', 'intent/1')).toBe('/index-network/intent/1')
  })

  it('builds the navigate path from nako://open/… payloads', () => {
    expect(pathFromOpenDeepLink('index-network/intent/1')).toBe('/index-network/intent/1')
    expect(pathFromNakoDeepLink('open', 'agent/42')).toBe('/agent/42')
  })

  it('ignores reserved kinds', () => {
    expect(pathFromNakoDeepLink('blueprint', 'morning-brief')).toBeNull()
    expect(pathFromNakoDeepLink('plugin', 'install')).toBeNull()
  })
})
