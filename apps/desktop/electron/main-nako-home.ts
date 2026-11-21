// The Nako home the desktop main process runs against, resolved once at boot.
// Resolving it here also registers it with the file-preview read guard, so the
// home's (and every profile's) credential stores are refused before any IPC
// read can be served.

import { resolveDesktopNakoHome } from './data-paths'
import { directoryExists } from './fs-probes'
import { setSensitiveNakoHome } from './hardening'
import { readWindowsUserEnvVar } from './windows-user-env'

export function resolveMainNakoHome(home: string): string {
  const nakoHome = resolveDesktopNakoHome({
    home,
    directoryExists,
    readWindowsHome: (): string | null => readWindowsUserEnvVar('NAKO_HOME')
  })

  setSensitiveNakoHome(nakoHome)

  return nakoHome
}
