import { describe, expect, it } from 'vitest'

import { commandLineIsNakoRuntime } from './nako-process'

// HX003: process identity comes from a whole argv TOKEN, never an argv substring.
describe('commandLineIsNakoRuntime (HX003 token matching)', () => {
  it('matches a real Nako gateway command line', () => {
    expect(commandLineIsNakoRuntime('nako\0gateway\0run')).toBe(true)
    expect(commandLineIsNakoRuntime('nako gateway run')).toBe(true)
    expect(commandLineIsNakoRuntime('nako --profile work gateway run')).toBe(true)
  })

  it('matches a Nako launched by an interpreter (basename of a path token)', () => {
    expect(commandLineIsNakoRuntime('/venv/bin/python\0/venv/bin/nako\0gateway\0run')).toBe(true)
    expect(commandLineIsNakoRuntime('C:\\Nako\\nako.exe gateway run')).toBe(true)
  })

  // The bug this replaces: a bare `.includes('nako')` matched any of these.
  it('does NOT match an unrelated process whose path merely contains "nako"', () => {
    expect(commandLineIsNakoRuntime('vim /home/user/nako-notes.txt')).toBe(false)
    expect(commandLineIsNakoRuntime('/opt/nakobrew/bin/tool --run')).toBe(false)
    expect(commandLineIsNakoRuntime('node /srv/nako-dashboard/server.js')).toBe(false)
    expect(commandLineIsNakoRuntime('cat ~/.nako/config.yaml')).toBe(false)
  })

  // G2 (#150283): the real unmanaged-gateway launch is `python -m nako_cli.main … gateway
  // run`, which the executable-basename-only matcher REJECTED — so findRunningGatewayProfiles
  // missed a live gateway at first launch / profile repair. Mirror the canonical Python matcher
  // (gateway.status.looks_like_gateway_command_line): recognize the module entry point, by argv
  // TOKENS not a substring, carrying the gateway subcommand.
  it('matches a module-launched gateway (python -m nako_cli.main … gateway run)', () => {
    expect(commandLineIsNakoRuntime('python\0-m\0nako_cli.main\0-p\0x\0gateway\0run')).toBe(true)
    expect(commandLineIsNakoRuntime('python -m nako_cli.main -p x gateway run')).toBe(true)
    expect(commandLineIsNakoRuntime('/venv/bin/python /venv/lib/nako_cli/main.py gateway run')).toBe(true)
    expect(commandLineIsNakoRuntime('python -m nako_cli.main --profile work gateway run')).toBe(true)
  })

  it('does NOT match a module entry without the gateway subcommand, nor an unrelated nako-tool path', () => {
    // A nako_cli.main invocation that is not a gateway (e.g. `chat`) is not a running gateway.
    expect(commandLineIsNakoRuntime('python -m nako_cli.main chat')).toBe(false)
    // HX003: no substring; an unrelated tool whose path contains "nako" is never a runtime.
    expect(commandLineIsNakoRuntime('/opt/nako-tool/foo gateway run')).toBe(false)
    // `-p gateway` is a profile value, not a subcommand.
    expect(commandLineIsNakoRuntime('python -m nako_cli.main -p gateway chat')).toBe(false)
  })
})
