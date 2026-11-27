import { describe, expect, it } from 'vitest'

import { ephemeralMacBundleLocation, isAppTranslocated, resolveMacAppBundle } from './mac-bundle-location'

describe('resolveMacAppBundle', () => {
  it('resolves packaged .app bundles on macOS including App Translocation paths', () => {
    expect(resolveMacAppBundle('/Applications/Nako Agent.app/Contents/MacOS/Nako Agent', 'darwin')).toBe(
      '/Applications/Nako Agent.app'
    )
    expect(
      resolveMacAppBundle(
        '/private/var/folders/ab/cd/T/AppTranslocation/1F2E3D4C/d/Nako Agent.app/Contents/MacOS/Nako Agent',
        'darwin'
      )
    ).toBe('/private/var/folders/ab/cd/T/AppTranslocation/1F2E3D4C/d/Nako Agent.app')
  })

  it('returns null off macOS or for non-bundle executables', () => {
    expect(resolveMacAppBundle('/Applications/Nako Agent.app/Contents/MacOS/Nako Agent', 'linux')).toBeNull()
    expect(resolveMacAppBundle('/Applications/Nako Agent.app/Contents/MacOS/Nako Agent', 'win32')).toBeNull()
    expect(resolveMacAppBundle('/usr/local/bin/electron', 'darwin')).toBeNull()
    expect(resolveMacAppBundle('', 'darwin')).toBeNull()
  })
})

describe('ephemeralMacBundleLocation', () => {
  const readOnly = () => {
    throw Object.assign(new Error('read-only file system'), { code: 'EROFS' })
  }

  it('reports a translocated copy and a read-only mount, and leaves a root-owned folder to Squirrel', () => {
    const translocated = '/private/var/folders/x/T/AppTranslocation/9A8B/d/Nako Agent.app'

    expect(isAppTranslocated(translocated)).toBe(true)
    expect(ephemeralMacBundleLocation(translocated, () => undefined)).toBe('app-translocation')
    expect(ephemeralMacBundleLocation('/Volumes/Nako Agent/Nako Agent.app', readOnly)).toBe('read-only-volume')
    expect(
      ephemeralMacBundleLocation('/opt/nako/Nako Agent.app', () => {
        throw Object.assign(new Error('permission denied'), { code: 'EACCES' })
      })
    ).toBeNull()
  })

  it('knows nothing of the prompt-only Downloads and Desktop rules', () => {
    for (const folder of ['Downloads', 'Desktop']) {
      const bundle = `/Users/alice/${folder}/Nako Agent.app`

      expect(isAppTranslocated(bundle)).toBe(false)
      expect(ephemeralMacBundleLocation(bundle, () => undefined)).toBeNull()
    }
  })
})
