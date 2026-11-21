import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  appendUniquePathEntries,
  buildDesktopBackendEnv,
  normalizeNakoHomeRoot,
  pathEnvKey,
  POSIX_SANE_PATH_ENTRIES,
  profileBackendParentEnv
} from './backend-env'
import { applyLoginShellPath } from './shell-path'

test('backend env scrubs PYTHONPATH and PYTHONHOME', () => {
  const env = buildDesktopBackendEnv({
    currentEnv: {
      PATH: '/usr/bin:/bin',
      PYTHONPATH: '/leaked/other/checkout',
      PYTHONHOME: '/leaked/python'
    },
    platform: 'darwin'
  })

  assert.equal(env.PYTHONPATH, '')
  assert.equal(env.PYTHONHOME, '')
})

test('POSIX backend PATH keeps the inherited PATH first and appends missing sane entries', () => {
  const env = buildDesktopBackendEnv({
    currentEnv: { PATH: '/opt/homebrew/bin:/usr/bin:/bin' },
    platform: 'darwin'
  })

  const entries = env.PATH.split(':')
  assert.equal(entries[0], '/opt/homebrew/bin', 'inherited PATH keeps precedence')
  assert.equal(entries.filter(entry => entry === '/opt/homebrew/bin').length, 1, 'no duplicates')

  for (const expected of POSIX_SANE_PATH_ENTRIES) {
    assert.ok(entries.includes(expected), `${expected} should be present`)
  }
})

test('backend runs the store toolchain even after the login-shell PATH is merged in front of it', async () => {
  // `nako desktop` hands Electron a PATH with the PM store first; the
  // login-shell merge then puts nvm/Homebrew ahead of it in process.env.
  const env: Record<string, string> = {
    PATH: '/Users/u/.nako/tools/node-26.7.0-darwin-arm64/bin:/Users/u/.nako/tools/uv-0.12.3-darwin-arm64:/usr/bin:/bin'
  }

  const loginPath = '/Users/u/.nvm/versions/node/v20.0.0/bin:/opt/homebrew/bin:/usr/bin'

  const execFileFn = (_file, _args, _options, callback) => {
    queueMicrotask(() => callback(null, `__NAKO_LOGIN_PATH_START__${loginPath}__NAKO_LOGIN_PATH_END__`, ''))

    return { stdin: { end() {} } }
  }

  await applyLoginShellPath({ env, platform: 'darwin', execFileFn })
  assert.equal(env.PATH.split(':')[0], '/Users/u/.nvm/versions/node/v20.0.0/bin', 'user-facing env keeps login order')

  const backend = buildDesktopBackendEnv({ currentEnv: env, platform: 'darwin', homedir: '/Users/u' })

  assert.deepEqual(backend.PATH.split(':').slice(0, 5), [
    '/Users/u/.nako/tools/node-26.7.0-darwin-arm64/bin',
    '/Users/u/.nako/tools/uv-0.12.3-darwin-arm64',
    '/Users/u/.nvm/versions/node/v20.0.0/bin',
    '/opt/homebrew/bin',
    '/usr/bin'
  ])
})

test('NAKO_RUNTIME_DIR names the store; look-alike prefixes are not Nako-owned', () => {
  const store = '/Applications/Nako.app/Contents/Resources/agent-payload/tools'

  const backend = buildDesktopBackendEnv({
    currentEnv: {
      NAKO_RUNTIME_DIR: store,
      PATH: `/opt/homebrew/bin:/Users/u/.nako/tools-old/bin:${store}/npm-12.0.2-darwin-arm64/bin:/usr/bin`
    },
    platform: 'darwin',
    homedir: '/Users/u'
  })

  assert.deepEqual(backend.PATH.split(':').slice(0, 4), [
    `${store}/npm-12.0.2-darwin-arm64/bin`,
    '/opt/homebrew/bin',
    '/Users/u/.nako/tools-old/bin',
    '/usr/bin'
  ])
})

test('Windows PATH casing and delimiter are preserved without POSIX sane entries', () => {
  const env = buildDesktopBackendEnv({
    currentEnv: { Path: 'C:\\Windows\\System32;C:\\Windows' },
    platform: 'win32'
  })

  assert.equal(env.Path, 'C:\\Windows\\System32;C:\\Windows')
  assert.equal(env.PATH, undefined)
})

test('buildDesktopBackendEnv forces PYTHONUTF8 unless the user set it explicitly', () => {
  const defaulted = buildDesktopBackendEnv({
    currentEnv: { PATH: '/usr/bin' },
    platform: 'darwin'
  })

  assert.equal(defaulted.PYTHONUTF8, '1')

  const optedOut = buildDesktopBackendEnv({
    currentEnv: { PATH: '/usr/bin', PYTHONUTF8: '0' },
    platform: 'darwin'
  })

  assert.equal(optedOut.PYTHONUTF8, '0')
})

test('normalizeNakoHomeRoot expands a literal leading ~ against the home directory, not cwd', () => {
  assert.equal(
    normalizeNakoHomeRoot('~/.nako', { pathModule: path.posix, homedir: '/Users/test' }),
    '/Users/test/.nako'
  )
  assert.equal(
    normalizeNakoHomeRoot('~/.nako/profiles/oracle', { pathModule: path.posix, homedir: '/Users/test' }),
    '/Users/test/.nako'
  )
  assert.equal(
    normalizeNakoHomeRoot('~\\.nako', { pathModule: path.win32, homedir: 'C:\\Users\\test' }),
    'C:\\Users\\test\\.nako'
  )
  assert.equal(normalizeNakoHomeRoot('~', { pathModule: path.posix, homedir: '/Users/test' }), '/Users/test')
})

test('normalizeNakoHomeRoot maps profile homes back to the global Nako root', () => {
  assert.equal(
    normalizeNakoHomeRoot('/Users/test/.nako/profiles/oracle', { pathModule: path.posix }),
    '/Users/test/.nako'
  )
  assert.equal(
    normalizeNakoHomeRoot('C:\\Users\\test\\AppData\\Local\\nako\\profiles\\oracle', { pathModule: path.win32 }),
    'C:\\Users\\test\\AppData\\Local\\nako'
  )
  assert.equal(normalizeNakoHomeRoot('/Users/test/.nako', { pathModule: path.posix }), '/Users/test/.nako')
})

test('pathEnvKey finds the platform-cased PATH key', () => {
  assert.equal(pathEnvKey({ Path: 'x' }, 'win32'), 'Path')
  assert.equal(pathEnvKey({ PATH: 'x' }, 'win32'), 'PATH')
  assert.equal(pathEnvKey({}, 'win32'), 'PATH')
  assert.equal(pathEnvKey({ Path: 'x' }, 'darwin'), 'PATH')
})

test('appendUniquePathEntries flattens, dedupes, and preserves first occurrence', () => {
  assert.equal(appendUniquePathEntries(['/a:/b', ['/b', '/c'], '', null], { delimiter: ':' }), '/a:/b:/c')
})

// `nako desktop` loads its launch profile's .env/.op.env into os.environ and
// hands that env to Electron; these cover what a profile backend inherits (#68367).
function withNakoRoot(files: Record<string, string>, run: (root: string) => void) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'nako-profile-env-'))

  try {
    for (const [rel, contents] of Object.entries(files)) {
      fs.mkdirSync(path.dirname(path.join(root, rel)), { recursive: true })
      fs.writeFileSync(path.join(root, rel), contents)
    }

    run(root)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

const ROOT_SCOPE_FILES = {
  '.env': '\uFEFFTLON_SHIP_URL=https://moon.invalid\nexport TLON_SHIP_CODE = "root-code" # moon\nPATH=/root/bin\n',
  '.op.env': 'OP_SERVICE_ACCOUNT_TOKEN=root-op\n',
  'profiles/urbot/.env': 'ANTHROPIC_API_KEY=urbot-key\n'
}

const ROOT_LAUNCHED_ENV = {
  HOME: '/Users/test',
  PATH: '/usr/bin:/bin',
  TLON_SHIP_URL: 'https://moon.invalid',
  TLON_SHIP_CODE: 'root-code',
  OP_SERVICE_ACCOUNT_TOKEN: 'root-op',
  OPENROUTER_API_KEY: 'shell-key'
}

test('a named profile backend does not inherit secrets the root .env/.op.env loaded into Desktop', () => {
  withNakoRoot(ROOT_SCOPE_FILES, root => {
    const env = profileBackendParentEnv({
      nakoHome: root,
      profile: 'urbot',
      currentEnv: ROOT_LAUNCHED_ENV,
      platform: 'linux'
    })

    // Shell exports the root dotenv never declared, and OS names, still reach the child.
    assert.deepEqual(env, { HOME: '/Users/test', PATH: '/usr/bin:/bin', OPENROUTER_API_KEY: 'shell-key' })
  })
})

test('a named profile backend keeps the launch NAKO_MANAGED_DIR when the root .env plants one', () => {
  withNakoRoot({ ...ROOT_SCOPE_FILES, '.env': 'NAKO_MANAGED_DIR=/home/user/planted\n' }, root => {
    const currentEnv = { ...ROOT_LAUNCHED_ENV, NAKO_MANAGED_DIR: '/etc/org-policy' }
    const env = profileBackendParentEnv({ nakoHome: root, profile: 'urbot', currentEnv, platform: 'linux' })

    // Launch-only (nako_constants.LAUNCH_ONLY_ENV_KEYS): the loader never took the .env line, so
    // the value in Desktop's env is the administrator's, not launch-profile residue.
    assert.equal(env.NAKO_MANAGED_DIR, '/etc/org-policy')
  })
})

test('the launch profile backend inherits the Desktop env unchanged', () => {
  withNakoRoot(ROOT_SCOPE_FILES, root => {
    for (const profile of ['default', null, undefined]) {
      assert.deepEqual(
        profileBackendParentEnv({ nakoHome: root, profile, currentEnv: ROOT_LAUNCHED_ENV, platform: 'linux' }),
        ROOT_LAUNCHED_ENV
      )
    }
  })
})

test('a primary backend without an explicit profile follows the sticky active_profile', () => {
  withNakoRoot({ ...ROOT_SCOPE_FILES, active_profile: 'urbot\n' }, root => {
    const env = profileBackendParentEnv({ nakoHome: root, profile: null, currentEnv: ROOT_LAUNCHED_ENV })

    assert.equal(env.TLON_SHIP_CODE, undefined)
    assert.equal(env.OP_SERVICE_ACCOUNT_TOKEN, undefined)
    assert.equal(env.OPENROUTER_API_KEY, 'shell-key')
  })
})

test('Desktop launched from a named profile keeps that profile out of the default backend', () => {
  withNakoRoot(
    {
      '.env': 'OPENAI_API_KEY=root-key\n',
      'profiles/work/.env': 'TLON_SHIP_CODE=work-code\nOP_SERVICE_ACCOUNT_TOKEN=work-op\n'
    },
    root => {
      const currentEnv = {
        NAKO_HOME: path.join(root, 'profiles', 'work'),
        TLON_SHIP_CODE: 'work-code',
        OP_SERVICE_ACCOUNT_TOKEN: 'work-op',
        OPENAI_API_KEY: 'shell-key'
      }

      assert.deepEqual(profileBackendParentEnv({ nakoHome: root, profile: 'default', currentEnv }), {
        NAKO_HOME: currentEnv.NAKO_HOME,
        OPENAI_API_KEY: 'shell-key'
      })
      assert.deepEqual(profileBackendParentEnv({ nakoHome: root, profile: 'work', currentEnv }), currentEnv)
    }
  )
})

test('Windows matches profile homes and dotenv names case-insensitively', () => {
  const root = 'C:\\Users\\test\\AppData\\Local\\nako'
  const files = { [`${root}\\.env`]: 'TELEGRAM_BOT_TOKEN=root-token\r\n' }

  const fsModule = {
    readFileSync: file => {
      if (!(file in files)) {
        throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' })
      }

      return files[file]
    }
  }

  const currentEnv = {
    NAKO_HOME: 'c:\\users\\test\\appdata\\local\\NAKO',
    Path: 'C:\\Windows',
    Telegram_Bot_Token: 'root-token'
  }

  const scoped = (profile: string) =>
    profileBackendParentEnv({ nakoHome: root, profile, currentEnv, platform: 'win32', fsModule })

  assert.deepEqual(scoped('default'), currentEnv)
  assert.deepEqual(scoped('urbot'), { NAKO_HOME: currentEnv.NAKO_HOME, Path: 'C:\\Windows' })
})
