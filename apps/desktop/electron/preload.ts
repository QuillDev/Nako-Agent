import { contextBridge, ipcRenderer, webFrame, webUtils } from 'electron'

import type { DesktopProfileRoute } from './desktop-profile'
import type { HudModifierApi, HudModifierStatus } from './hud-modifier-types'
import { customWindowControlsEnabled } from './window-controls'

// Which translucency the OS can back. Asked synchronously because the renderer
// needs it before its first paint, and answered by main because deciding it
// needs `os.release()` — a sandboxed preload may only require electron, events,
// timers and url, so importing node:os here throws before contextBridge runs
// and takes the ENTIRE bridge down with it (window.nakoDesktop undefined =>
// "Desktop IPC bridge is unavailable"). No reply means no glass, which degrades
// to an ordinary opaque window rather than a page thinned over nothing.
const translucencySupport = ipcRenderer.sendSync('nako:translucency:support')
const hudWindowing = ipcRenderer.sendSync('nako:hud:windowing')
const hudNativeDrag = hudWindowing?.nativeDrag === true

const launchFlags: { localModels?: boolean; assumeFreshInstall?: boolean } | undefined =
  ipcRenderer.sendSync('nako:feature-flags')

// Local, sanitized skin payload for the first renderer theme paint. This does
// not wait on `gateway.ready`, so an unreachable remote primary cannot force
// the built-in palette over the skin configured on this machine.
const localSkin = ipcRenderer.sendSync('nako:skin:local')

// This machine's UI preferences store (#145213), read before the first paint so
// a preference set in another window or in this machine's Webapp paints at once.
const uiPreferencesSnapshot = ipcRenderer.sendSync('nako:ui-preferences:snapshot')

import { unwrapExpectedNotFound } from './api-expected-404'

contextBridge.exposeInMainWorld('nakoDesktop', {
  glassSupported: translucencySupport?.glass === true,
  translucencySupported: translucencySupport?.translucency === true,
  // Launch-flag fact: the app was started with --local, so the renderer may
  // show the local-models surfaces. Static for the window's lifetime.
  localModelsEnabled: launchFlags?.localModels === true,
  // Dev-only launch fact: an unpackaged app started with --fresh-install, so
  // onboarding treats this launch as a fresh install. Always false when packaged.
  assumeFreshInstall: launchFlags?.assumeFreshInstall === true,
  localSkin: localSkin && typeof localSkin === 'object' ? localSkin : null,
  uiPreferences: {
    snapshot: uiPreferencesSnapshot && typeof uiPreferencesSnapshot === 'object' ? uiPreferencesSnapshot : null,
    read: () => ipcRenderer.invoke('nako:ui-preferences:read'),
    write: payload => ipcRenderer.invoke('nako:ui-preferences:write', payload),
    onChanged: callback => {
      const listener = (_event, snapshot) => callback(snapshot)
      ipcRenderer.on('nako:ui-preferences:changed', listener)

      return () => ipcRenderer.removeListener('nako:ui-preferences:changed', listener)
    }
  },
  getConnection: (profile, opts) => ipcRenderer.invoke('nako:connection', profile, opts),
  // Loopback origin that hosts YouTube's player for the file:// renderer.
  getEmbedHostOrigin: () => ipcRenderer.invoke('nako:embed-host:origin'),
  // Registry-scoped backend resolution: { connectionId, profile } → descriptor.
  getConnectionFor: payload => ipcRenderer.invoke('nako:connection:for', payload),
  getProfileRoutes: profiles => ipcRenderer.invoke('nako:plugin-profile-routes', profiles),
  revalidateConnection: () => ipcRenderer.invoke('nako:connection:revalidate'),
  getGatewayWsUrl: profile => ipcRenderer.invoke('nako:gateway:ws-url', profile),
  // Registry-scoped fresh WS URL: { connectionId, profile } → result shape of
  // getGatewayWsUrl, minted against that connection's backend.
  getGatewayWsUrlFor: payload => ipcRenderer.invoke('nako:gateway:ws-url-for', payload),
  // { connectionId, profile, pluginId, path } → one-use local plugin socket URL, null, or { refused: 'permission' }.
  getPluginWsUrl: payload => ipcRenderer.invoke('nako:plugin:ws-url', payload),
  // Union agent roster across every registered connection.
  getAgentRoster: () => ipcRenderer.invoke('nako:agents:roster'),
  openSessionWindow: (sessionId, opts) => ipcRenderer.invoke('nako:window:openSession', sessionId, opts),
  openSessionInTerminal: (sessionId, opts) => ipcRenderer.invoke('nako:window:openInTerminal', sessionId, opts),
  openWindow: (options?: DesktopProfileRoute) => ipcRenderer.invoke('nako:window:openInstance', options),
  openBrowserWindow: tabId => ipcRenderer.invoke('nako:window:openBrowser', tabId),
  windowRelay: {
    send: payload => ipcRenderer.send('nako:window:relay', payload),
    onMessage: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('nako:window:relay', listener)

      return () => ipcRenderer.removeListener('nako:window:relay', listener)
    }
  },
  onBrowserPopoutClosed: callback => {
    const listener = (_event, tabId) => callback(tabId)
    ipcRenderer.on('nako:browser-popout:closed', listener)

    return () => ipcRenderer.removeListener('nako:browser-popout:closed', listener)
  },
  claimAmbientCue: key => ipcRenderer.invoke('nako:ambient:claim', key),
  windowControls: {
    custom: customWindowControlsEnabled(),
    minimize: () => ipcRenderer.send('nako:window-control', 'minimize'),
    toggleMaximize: () => ipcRenderer.send('nako:window-control', 'toggle-maximize'),
    close: () => ipcRenderer.send('nako:window-control', 'close')
  },
  wakeIndicator: {
    getState: () => ipcRenderer.invoke('nako:wake-indicator:get'),
    setState: state => ipcRenderer.send('nako:wake-indicator:set', state),
    onState: callback => {
      const listener = (_event, state) => callback(state)
      ipcRenderer.on('nako:wake-indicator:state', listener)

      return () => ipcRenderer.removeListener('nako:wake-indicator:state', listener)
    }
  },
  chatOnboarding: {
    size: mode => ipcRenderer.send('nako:window:size', mode)
  },
  petOverlay: {
    // Main renderer → main process: window lifecycle + drag. `request` is
    // `{ bounds, screen }`; resolves with the screen bounds it actually used.
    open: request => ipcRenderer.invoke('nako:pet-overlay:open', request),
    close: () => ipcRenderer.invoke('nako:pet-overlay:close'),
    setBounds: bounds => ipcRenderer.send('nako:pet-overlay:set-bounds', bounds),
    setIgnoreMouse: ignore => ipcRenderer.send('nako:pet-overlay:ignore-mouse', ignore),
    // Flip the overlay focusable (and focus it) while the composer needs keys.
    setFocusable: focusable => ipcRenderer.send('nako:pet-overlay:set-focusable', focusable),
    // Main renderer → overlay (forwarded by main): push the latest pet state.
    pushState: payload => ipcRenderer.send('nako:pet-overlay:state', payload),
    // Overlay → main renderer (forwarded by main): pop back in / composer submit.
    control: payload => ipcRenderer.send('nako:pet-overlay:control', payload),
    // Overlay subscribes to state pushes.
    onState: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('nako:pet-overlay:state', listener)

      return () => ipcRenderer.removeListener('nako:pet-overlay:state', listener)
    },
    // Main renderer subscribes to overlay control messages.
    onControl: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('nako:pet-overlay:control', listener)

      return () => ipcRenderer.removeListener('nako:pet-overlay:control', listener)
    }
  },
  // HUD mode: the chrome-free floating chat. A full app renderer (own gateway)
  // sized as a floating bar, so it mounts the real composer. Main owns the
  // window; `onChanged` keeps every window's toggle truthful.
  hud: {
    nativeDrag: hudNativeDrag,
    windowing: {
      clientPlacement: hudWindowing?.clientPlacement !== false,
      controlDrag: hudWindowing?.controlDrag === true,
      nativeDrag: hudNativeDrag,
      solid: hudWindowing?.solid === true,
      workspaceTransfer: hudWindowing?.workspaceTransfer === true
    },
    open: request => ipcRenderer.invoke('nako:hud:open', request),
    close: () => ipcRenderer.invoke('nako:hud:close'),
    setIgnoreMouse: ignore => ipcRenderer.send('nako:hud:ignore-mouse', ignore),
    beginMove: () => ipcRenderer.send('nako:hud:begin-move'),
    endMove: () => ipcRenderer.send('nako:hud:end-move'),
    moveBy: delta => ipcRenderer.send('nako:hud:move-by', delta),
    setWorkspaceTransfer: transferring => ipcRenderer.send('nako:hud:workspace-transfer', transferring),
    setBounds: bounds => ipcRenderer.send('nako:hud:set-bounds', bounds),
    resetLayout: () => ipcRenderer.invoke('nako:hud:reset-layout'),
    // Whether the band covers the window below the bar. Main pairs it with the
    // user's translucency setting to decide the native frost (macOS vibrancy /
    // Windows 11 DWM backdrop) — see hudFrostFor.
    setFrost: showing => ipcRenderer.invoke('nako:hud:frost', showing),
    // The HUD tells main which session it is on; main hands that back to the
    // app window when the HUD closes, so the app can re-home onto it.
    setSession: sessionId => ipcRenderer.send('nako:hud:session', sessionId),
    onGoto: callback => {
      const listener = (_event, sessionId) => callback(sessionId)
      ipcRenderer.on('nako:hud:goto', listener)

      return () => ipcRenderer.removeListener('nako:hud:goto', listener)
    },
    onChanged: callback => {
      const listener = (_event, state) => callback(state)
      ipcRenderer.on('nako:hud:changed', listener)

      return () => ipcRenderer.removeListener('nako:hud:changed', listener)
    },
    // Linux only, and silent elsewhere: where the cursor is, in page
    // coordinates, or null when it has left the window. Stands in for the
    // mousemove that `setIgnoreMouseEvents(true, { forward: true })` delivers on
    // macOS and Windows but not here.
    onCursor: callback => {
      const listener = (_event, point) => callback(point)
      ipcRenderer.on('nako:hud:cursor', listener)

      return () => ipcRenderer.removeListener('nako:hud:cursor', listener)
    },
    // Main's game-overlay watch: whether a fullscreen app (a game) is under
    // the HUD, so the renderer can step back to the low-opacity overlay
    // treatment while one owns the screen.
    onGameOverlay: callback => {
      const listener = (_event, state) => callback(state)
      ipcRenderer.on('nako:hud:game-overlay', listener)

      return () => ipcRenderer.removeListener('nako:hud:game-overlay', listener)
    }
  },
  hudModifier: {
    getSettings: () => ipcRenderer.invoke('nako:hud-modifier:settings:get'),
    setEnabled: enabled => ipcRenderer.invoke('nako:hud-modifier:settings:set', enabled),
    openPermissionSettings: () => ipcRenderer.invoke('nako:hud-modifier:permission'),
    onStatus: callback => {
      const listener = (_event: Electron.IpcRendererEvent, status: HudModifierStatus) => callback(status)
      ipcRenderer.on('nako:hud-modifier:status', listener)

      return () => ipcRenderer.removeListener('nako:hud-modifier:status', listener)
    }
  } satisfies HudModifierApi,
  // macOS native screenshot gesture; captures require a main-issued request.
  screenshot:
    process.platform === 'darwin'
      ? {
          getSettings: () => ipcRenderer.invoke('nako:screenshot:settings:get'),
          setEnabled: enabled => ipcRenderer.invoke('nako:screenshot:settings:set', enabled),
          openPermissionSettings: kind => ipcRenderer.invoke('nako:screenshot:permission', kind),
          capture: requestId => ipcRenderer.invoke('nako:screenshot:capture', requestId),
          onStatus: callback => {
            const listener = (_event, status) => callback(status)
            ipcRenderer.on('nako:screenshot:status', listener)

            return () => ipcRenderer.removeListener('nako:screenshot:status', listener)
          },
          onRequest: callback => {
            const channel = 'nako:screenshot:request'
            const listener = (_event, requestId) => callback(requestId)

            if (ipcRenderer.listenerCount(channel) === 0) {
              ipcRenderer.send('nako:screenshot:subscribe', true)
            }

            ipcRenderer.on(channel, listener)

            return () => {
              ipcRenderer.removeListener(channel, listener)

              if (ipcRenderer.listenerCount(channel) === 0) {
                ipcRenderer.send('nako:screenshot:subscribe', false)
              }
            }
          }
        }
      : undefined,
  // Quick Entry: the global-hotkey mini composer window. Main owns the OS
  // shortcut + the persisted preference; the quick window only captures text
  // and hands it back, and the primary renderer submits it through the normal
  // prompt path.
  quickEntry: {
    getSettings: () => ipcRenderer.invoke('nako:quick-entry:settings:get'),
    setSettings: patch => ipcRenderer.invoke('nako:quick-entry:settings:set', patch),
    // Invoke returns the delivery result so the draft is not lost (#85590).
    submit: payload => ipcRenderer.invoke('nako:quick-entry:submit', payload),
    // Main cannot invoke the primary renderer, so it receives this ack (#85590).
    ackSubmit: (correlationId, result) => ipcRenderer.send('nako:quick-entry:ack', { correlationId, result }),
    dismiss: () => ipcRenderer.send('nako:quick-entry:dismiss'),
    // Primary renderer → main → quick window: gateway connection state + the
    // recent-session options the target picker offers. Main caches the latest
    // payload so a freshly spawned quick window starts from truth.
    pushState: payload => ipcRenderer.send('nako:quick-entry:state', payload),
    // Quick window subscribes to those pushes.
    onState: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('nako:quick-entry:state', listener)

      return () => ipcRenderer.removeListener('nako:quick-entry:state', listener)
    },
    // Main → primary renderer: a submit captured by the quick window.
    onSubmit: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('nako:quick-entry:submit', listener)

      return () => ipcRenderer.removeListener('nako:quick-entry:submit', listener)
    },
    // Main → quick window: you were just summoned (reset draft + refocus).
    onShown: callback => {
      const listener = () => callback()
      ipcRenderer.on('nako:quick-entry:shown', listener)

      return () => ipcRenderer.removeListener('nako:quick-entry:shown', listener)
    },
    // Main → quick window: the outcome of a submit whose relay already timed
    // out. Delivery is now KNOWN — reconcile the unknown state instead of
    // leaving the user to resend a prompt that may already be delivered.
    onLateResult: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('nako:quick-entry:late-result', listener)

      return () => ipcRenderer.removeListener('nako:quick-entry:late-result', listener)
    }
  },
  getBootProgress: () => ipcRenderer.invoke('nako:boot-progress:get'),
  getConnectionConfig: profile => ipcRenderer.invoke('nako:connection-config:get', profile),
  saveConnectionConfig: payload => ipcRenderer.invoke('nako:connection-config:save', payload),
  applyConnectionConfig: payload => ipcRenderer.invoke('nako:connection-config:apply', payload),
  testConnectionConfig: payload => ipcRenderer.invoke('nako:connection-config:test', payload),
  // Opt-in OS-keychain encryption for stored gateway secrets (default off —
  // see secret-storage-policy.ts). get never touches the OS keychain.
  getSecretStorageEncryption: () => ipcRenderer.invoke('nako:secret-storage:get'),
  setSecretStorageEncryption: (on: boolean) => ipcRenderer.invoke('nako:secret-storage:set', on),
  // v2 multi-connection registry: named agent sources (local / remote / cloud / ssh).
  connections: {
    list: () => ipcRenderer.invoke('nako:connections:list'),
    save: payload => ipcRenderer.invoke('nako:connections:save', payload),
    remove: id => ipcRenderer.invoke('nako:connections:remove', id),
    setPrimary: id => ipcRenderer.invoke('nako:connections:set-primary', id),
    setLaunchMode: mode => ipcRenderer.invoke('nako:connections:set-launch-mode', mode),
    setLastUsed: id => ipcRenderer.invoke('nako:connections:set-last-used', id),
    test: id => ipcRenderer.invoke('nako:connections:test', id),
    updateManaged: id => ipcRenderer.invoke('nako:connections:update-managed', id),
    // Fan out `nako update` to every eligible registered connection.
    // Optional excludeIds skips rows the caller updates through another path.
    updateAll: options => ipcRenderer.invoke('nako:connections:update-all', options),
    // Registry lifecycle push (main → renderer): a connection was removed or
    // materially edited, so secondaries scoped to it must be disposed (and,
    // for edits, re-dialed at the new target).
    onChanged: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('nako:connections:changed', listener)

      return () => ipcRenderer.removeListener('nako:connections:changed', listener)
    }
  },
  sshConfigHosts: () => ipcRenderer.invoke('nako:ssh-config:hosts'),
  sshResolveHost: host => ipcRenderer.invoke('nako:ssh-config:resolve', host),
  probeConnectionConfig: remoteUrl => ipcRenderer.invoke('nako:connection-config:probe', remoteUrl),
  // `options` lets a registry-editor draft sign in BEFORE it is saved: the
  // main process settles the draft's connection id up front so the login
  // window writes into the per-connection cookie jar the saved entry will
  // read (not the legacy shared jar an unsaved URL would fall back to).
  oauthLoginConnectionConfig: (remoteUrl, options) =>
    ipcRenderer.invoke('nako:connection-config:oauth-login', remoteUrl, options),
  oauthLogoutConnectionConfig: remoteUrl => ipcRenderer.invoke('nako:connection-config:oauth-logout', remoteUrl),
  // Nako Cloud: one portal login powers discovery + silent per-agent sign-in
  // (cloud-auto-discovery Phase 3).
  cloud: {
    status: () => ipcRenderer.invoke('nako:cloud:status'),
    login: () => ipcRenderer.invoke('nako:cloud:login'),
    logout: () => ipcRenderer.invoke('nako:cloud:logout'),
    discover: org => ipcRenderer.invoke('nako:cloud:discover', org),
    agentSignIn: dashboardUrl => ipcRenderer.invoke('nako:cloud:agent-sign-in', dashboardUrl)
  },
  profile: {
    getDefault: () => ipcRenderer.invoke('nako:profile:default:get'),
    setDefault: (route: DesktopProfileRoute) => ipcRenderer.invoke('nako:profile:default:set', route),
    onDefaultChanged: (callback: (route: DesktopProfileRoute | null) => void) => {
      const listener = (_event: Electron.IpcRendererEvent, route: DesktopProfileRoute | null) => callback(route)
      ipcRenderer.on('nako:profile:default:changed', listener)

      return () => ipcRenderer.removeListener('nako:profile:default:changed', listener)
    },
    get: () => ipcRenderer.invoke('nako:profile:get'),
    remember: name => ipcRenderer.invoke('nako:profile:remember', name),
    importConnections: (record: Record<string, string>) =>
      ipcRenderer.invoke('nako:profile:connections:import', record),
    rememberConnection: (connectionId: string, profile: string) =>
      ipcRenderer.invoke('nako:profile:connections:remember', connectionId, profile),
    set: name => ipcRenderer.invoke('nako:profile:set', name)
  },
  // The handler resolves an expected 404 with a sentinel instead of rejecting
  // (Electron logs a stack for every rejected invoke). Turn it back into the
  // rejection the renderer expects — see electron/api-expected-404.ts.
  api: request => ipcRenderer.invoke('nako:api', request).then(unwrapExpectedNotFound),
  notify: payload => ipcRenderer.invoke('nako:notify', payload),
  claimStartupLatency: () => ipcRenderer.invoke('nako:startup-latency:claim'),
  requestMicrophoneAccess: () => ipcRenderer.invoke('nako:requestMicrophoneAccess'),
  readWindowBelow: () => ipcRenderer.invoke('nako:window:readBelow'),
  readFileDataUrl: filePath => ipcRenderer.invoke('nako:readFileDataUrl', filePath),
  readFileDataUrlForAttach: filePath => ipcRenderer.invoke('nako:readFileDataUrlForAttach', filePath),
  dataUrlReadMax: {
    get: () => ipcRenderer.invoke('nako:data-url-read-max:get'),
    set: maxMb => ipcRenderer.invoke('nako:data-url-read-max:set', maxMb)
  },
  readFileText: filePath => ipcRenderer.invoke('nako:readFileText', filePath),
  readPluginSource: (filePath: string) => ipcRenderer.invoke('nako:readPluginSource', filePath),
  selectPaths: options => ipcRenderer.invoke('nako:selectPaths', options),
  selectSavePath: options => ipcRenderer.invoke('nako:selectSavePath', options),
  saveTextFileAs: request => ipcRenderer.invoke('nako:saveTextFileAs', request),
  writeClipboard: text => ipcRenderer.invoke('nako:writeClipboard', text),
  readClipboard: () => ipcRenderer.invoke('nako:readClipboard'),
  saveGatewayFile: payload => ipcRenderer.invoke('nako:saveGatewayFile', payload),
  saveImageFromUrl: url => ipcRenderer.invoke('nako:saveImageFromUrl', url),
  contextMenuEdit: command => ipcRenderer.invoke('nako:context-menu:edit', command),
  contextMenuCopyImage: () => ipcRenderer.invoke('nako:context-menu:copy-image'),
  contextMenuSpellcheck: action => ipcRenderer.invoke('nako:context-menu:spellcheck', action),
  contextMenuGuestAddWord: payload => ipcRenderer.invoke('nako:context-menu:guest-add-word', payload),
  spellcheckDictionary: (op, word) => ipcRenderer.invoke('nako:spellcheck:dictionary', { op, word }),
  onContextMenuSpellcheck: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('nako:context-menu-spellcheck', listener)

    return () => ipcRenderer.removeListener('nako:context-menu-spellcheck', listener)
  },
  saveImageBuffer: (data, ext, name) => ipcRenderer.invoke('nako:saveImageBuffer', { data, ext, name }),
  capturePreview: payload => ipcRenderer.invoke('nako:capturePreview', payload),
  savePastedText: text => ipcRenderer.invoke('nako:savePastedText', { text }),
  saveClipboardImage: () => ipcRenderer.invoke('nako:saveClipboardImage'),
  getPathForFile: file => {
    try {
      return webUtils.getPathForFile(file) || ''
    } catch {
      return ''
    }
  },
  normalizePreviewTarget: (target, baseDir) => ipcRenderer.invoke('nako:normalizePreviewTarget', target, baseDir),
  watchPreviewFile: url => ipcRenderer.invoke('nako:watchPreviewFile', url),
  watchDirectory: dir => ipcRenderer.invoke('nako:watchDirectory', dir),
  stopPreviewFileWatch: id => ipcRenderer.invoke('nako:stopPreviewFileWatch', id),
  setActiveWork: payload => ipcRenderer.send('nako:active-work', payload),
  scheduleStreamWorkWake: (token, delayMs) => ipcRenderer.send('nako:stream-work-wake', { token, delayMs }),
  onStreamWorkWake: callback => {
    const listener = (_event, token) => callback(token)
    ipcRenderer.on('nako:stream-work-ready', listener)

    return () => ipcRenderer.removeListener('nako:stream-work-ready', listener)
  },
  setTitleBarTheme: payload => ipcRenderer.send('nako:titlebar-theme', payload),
  setNativeTheme: mode => ipcRenderer.send('nako:native-theme', mode),
  setTranslucency: payload => ipcRenderer.send('nako:translucency', payload),
  setKeepAwake: mode => ipcRenderer.send('nako:keep-awake', mode),
  // Once per computer, after a timed sleep interrupted a turn with keep-awake off.
  onKeepAwakeOffer: callback => {
    const listener = () => callback()
    ipcRenderer.on('nako:keep-awake-offer', listener)

    return () => ipcRenderer.removeListener('nako:keep-awake-offer', listener)
  },
  minimizeToTray: {
    get: () => ipcRenderer.invoke('nako:minimize-to-tray:get'),
    set: on => ipcRenderer.invoke('nako:minimize-to-tray:set', on),
    onChanged: callback => {
      const listener = (_event, status) => callback(status)
      ipcRenderer.on('nako:minimize-to-tray:changed', listener)

      return () => ipcRenderer.removeListener('nako:minimize-to-tray:changed', listener)
    }
  },
  setDisableF12: blocked => ipcRenderer.send('nako:devtools:disable-f12', blocked),
  setF12ShortcutActive: active => ipcRenderer.send('nako:f12ShortcutActive', Boolean(active)),
  onF12Shortcut: callback => {
    const listener = (_event, input) => callback(input)
    ipcRenderer.on('nako:f12-shortcut', listener)

    return () => ipcRenderer.removeListener('nako:f12-shortcut', listener)
  },
  setPreviewShortcutActive: active => ipcRenderer.send('nako:previewShortcutActive', Boolean(active)),
  setPreviewGuestHidden: (webContentsId, hidden) =>
    ipcRenderer.send('nako:preview-guest-hidden', { webContentsId, hidden: Boolean(hidden) }),
  beginPreviewGuestActivity: webContentsId => ipcRenderer.invoke('nako:preview-guest-activity:begin', webContentsId),
  endPreviewGuestActivity: token => ipcRenderer.invoke('nako:preview-guest-activity:end', token),
  executePreviewGuestScript: (token, code) => ipcRenderer.invoke('nako:preview-guest-activity:script', token, code),
  syncPreviewSignins: request => ipcRenderer.invoke('nako:preview-signins:sync', request),
  migratePreviewSigninsForProfile: (request: { oldProfile: string; newProfile: string }) =>
    ipcRenderer.invoke('nako:preview-signins:profile-renamed', request),
  dropPreviewSigninsForProfile: (request: { profile: string }) =>
    ipcRenderer.invoke('nako:preview-signins:profile-deleted', request),
  openExternal: url => ipcRenderer.invoke('nako:openExternal', url),
  freeTierChallenge: {
    // Load the account service's challenge page in a hidden window (revealed
    // only if the page asks for the human). Resolves with how it ended.
    run: request => ipcRenderer.invoke('nako:freeTierChallenge:run', request)
  },
  mcpOauth: {
    // One-shot loopback listener for MCP OAuth against remote backends: bind
    // on this machine, hand redirectUri to mcp.servers.oauth.start, then wait
    // for the provider redirect and relay code/state via oauth.callback.
    listen: () => ipcRenderer.invoke('nako:mcp-oauth:listen'),
    wait: (id, timeoutMs) => ipcRenderer.invoke('nako:mcp-oauth:wait', id, timeoutMs),
    cancel: id => ipcRenderer.invoke('nako:mcp-oauth:cancel', id)
  },
  openPreviewInBrowser: url => ipcRenderer.invoke('nako:openPreviewInBrowser', url),
  reachPreviewUrl: url => ipcRenderer.invoke('nako:preview:reach', url),
  setActiveConnectionRoute: route => ipcRenderer.send('nako:connection:active-route', route),
  fetchLinkTitle: url => ipcRenderer.invoke('nako:fetchLinkTitle', url),
  resolveFavicon: url => ipcRenderer.invoke('nako:resolveFavicon', url),
  sanitizeWorkspaceCwd: cwd => ipcRenderer.invoke('nako:workspace:sanitize', cwd),
  settings: {
    getDefaultProjectDir: () => ipcRenderer.invoke('nako:setting:defaultProjectDir:get'),
    setDefaultProjectDir: dir => ipcRenderer.invoke('nako:setting:defaultProjectDir:set', dir),
    pickDefaultProjectDir: () => ipcRenderer.invoke('nako:setting:defaultProjectDir:pick')
  },
  preparedSubmissions: {
    read: () => ipcRenderer.invoke('nako:prepared-submissions:read'),
    update: (key, entry) => ipcRenderer.invoke('nako:prepared-submissions:update', key, entry),
    compareAndSet: (key, expected, entry) =>
      ipcRenderer.invoke('nako:prepared-submissions:compare-and-set', key, expected, entry)
  },
  zoom: {
    // Current zoom of this window, as { level, percent }.
    get: () => ipcRenderer.invoke('nako:zoom:get'),
    // Synchronous zoom factor (1 = 100%). Coordinate math needs it in the
    // same tick as the event it converts, so no IPC round-trip here.
    factor: () => webFrame.getZoomFactor(),
    setPercent: percent => ipcRenderer.send('nako:zoom:set-percent', percent),
    // Fires on every zoom change, including the Ctrl/Cmd +/-/0 shortcuts,
    // so the settings UI can stay in sync with the keyboard.
    onChanged: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('nako:zoom:changed', listener)

      return () => ipcRenderer.removeListener('nako:zoom:changed', listener)
    }
  },
  revealLogs: () => ipcRenderer.invoke('nako:logs:reveal'),
  getRecentLogs: () => ipcRenderer.invoke('nako:logs:recent'),
  // Fire-and-forget: persists a renderer error-boundary catch (with component
  // stack) to desktop.log so crashes survive the window (#79428).
  reportRendererError: report => ipcRenderer.send('nako:logs:renderer-error', report),
  logLine: (line: string): void => ipcRenderer.send('nako:logs:renderer-line', line),
  readDir: dirPath => ipcRenderer.invoke('nako:fs:readDir', dirPath),
  gitRoot: startPath => ipcRenderer.invoke('nako:fs:gitRoot', startPath),
  revealPath: targetPath => ipcRenderer.invoke('nako:fs:reveal', targetPath),
  openDir: dirPath => ipcRenderer.invoke('nako:fs:openDir', dirPath),
  desktopPluginsRoot: () => ipcRenderer.invoke('nako:fs:desktopPluginsRoot'),
  reconcileDesktopPlugins: async () => (await ipcRenderer.invoke('nako:fs:reconcileDesktopPlugins')).published,
  reconcileDesktopPluginsReport: () => ipcRenderer.invoke('nako:fs:reconcileDesktopPlugins'),
  logsRoot: (profile?: string) => ipcRenderer.invoke('nako:fs:logsRoot', profile),
  renamePath: (targetPath, newName) => ipcRenderer.invoke('nako:fs:rename', targetPath, newName),
  writeTextFile: (filePath, content) => ipcRenderer.invoke('nako:fs:writeText', filePath, content),
  trashPath: targetPath => ipcRenderer.invoke('nako:fs:trash', targetPath),
  pluginFiles: {
    request: (pluginId, labels) => ipcRenderer.invoke('nako:pluginFiles:request', pluginId, labels),
    grants: pluginId => ipcRenderer.invoke('nako:pluginFiles:grants', pluginId),
    revoke: (pluginId, folderId) => ipcRenderer.invoke('nako:pluginFiles:revoke', pluginId, folderId),
    op: (pluginId, request) => ipcRenderer.invoke('nako:pluginFiles:op', pluginId, request),
    onChanged: callback => {
      const listener = (_event, change) => callback(change)
      ipcRenderer.on('nako:pluginFiles:changed', listener)

      return () => ipcRenderer.removeListener('nako:pluginFiles:changed', listener)
    }
  },
  git: {
    worktreeList: repoPath => ipcRenderer.invoke('nako:git:worktreeList', repoPath),
    worktreeAdd: (repoPath, options) => ipcRenderer.invoke('nako:git:worktreeAdd', repoPath, options),
    worktreeRemove: (repoPath, worktreePath, options) =>
      ipcRenderer.invoke('nako:git:worktreeRemove', repoPath, worktreePath, options),
    branchSwitch: (repoPath, branch) => ipcRenderer.invoke('nako:git:branchSwitch', repoPath, branch),
    branchList: repoPath => ipcRenderer.invoke('nako:git:branchList', repoPath),
    baseBranchList: repoPath => ipcRenderer.invoke('nako:git:baseBranchList', repoPath),
    repoStatus: repoPath => ipcRenderer.invoke('nako:git:repoStatus', repoPath),
    fileDiff: (repoPath, filePath) => ipcRenderer.invoke('nako:git:fileDiff', repoPath, filePath),
    scanRepos: (roots, options) => ipcRenderer.invoke('nako:git:scanRepos', roots, options),
    review: {
      list: (repoPath, scope, baseRef) => ipcRenderer.invoke('nako:git:review:list', repoPath, scope, baseRef),
      diff: (repoPath, filePath, scope, baseRef, staged) =>
        ipcRenderer.invoke('nako:git:review:diff', repoPath, filePath, scope, baseRef, staged),
      stage: (repoPath, filePath) => ipcRenderer.invoke('nako:git:review:stage', repoPath, filePath),
      unstage: (repoPath, filePath) => ipcRenderer.invoke('nako:git:review:unstage', repoPath, filePath),
      revert: (repoPath, filePath) => ipcRenderer.invoke('nako:git:review:revert', repoPath, filePath),
      revParse: (repoPath, ref) => ipcRenderer.invoke('nako:git:review:revParse', repoPath, ref),
      commit: (repoPath, message, push) => ipcRenderer.invoke('nako:git:review:commit', repoPath, message, push),
      commitContext: repoPath => ipcRenderer.invoke('nako:git:review:commitContext', repoPath),
      push: repoPath => ipcRenderer.invoke('nako:git:review:push', repoPath),
      shipInfo: repoPath => ipcRenderer.invoke('nako:git:review:shipInfo', repoPath),
      prList: (repoPath, branches, numbers) =>
        ipcRenderer.invoke('nako:git:review:prList', repoPath, branches, numbers),
      createPr: repoPath => ipcRenderer.invoke('nako:git:review:createPr', repoPath)
    }
  },
  terminal: {
    attach: id => ipcRenderer.invoke('nako:terminal:attach', id),
    cwd: id => ipcRenderer.invoke('nako:terminal:cwd', id),
    dispose: id => ipcRenderer.invoke('nako:terminal:dispose', id),
    resize: (id, size) => ipcRenderer.invoke('nako:terminal:resize', id, size),
    start: options => ipcRenderer.invoke('nako:terminal:start', options),
    write: (id, data) => ipcRenderer.invoke('nako:terminal:write', id, data),
    onData: (id, callback) => {
      const channel = `nako:terminal:${id}:data`
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on(channel, listener)

      return () => ipcRenderer.removeListener(channel, listener)
    },
    onExit: (id, callback) => {
      const channel = `nako:terminal:${id}:exit`
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on(channel, listener)

      return () => ipcRenderer.removeListener(channel, listener)
    }
  },
  onClosePreviewRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('nako:close-preview-requested', listener)

    return () => ipcRenderer.removeListener('nako:close-preview-requested', listener)
  },
  onPreviewNav: callback => {
    const listener = (_event, command) => callback(command)
    ipcRenderer.on('nako:preview-nav', listener)

    return () => ipcRenderer.removeListener('nako:preview-nav', listener)
  },
  onOpenFolderRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('nako:open-folder-requested', listener)

    return () => ipcRenderer.removeListener('nako:open-folder-requested', listener)
  },
  onOpenUpdatesRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('nako:open-updates', listener)

    return () => ipcRenderer.removeListener('nako:open-updates', listener)
  },
  onDeepLink: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('nako:deep-link', listener)

    return () => ipcRenderer.removeListener('nako:deep-link', listener)
  },
  signalDeepLinkReady: () => ipcRenderer.invoke('nako:deep-link-ready'),
  probePluginRepo: payload => ipcRenderer.invoke('nako:plugin:probe', payload),
  installDesktopPlugin: payload => ipcRenderer.invoke('nako:plugin:installDesktop', payload),
  removeDesktopPlugin: payload => ipcRenderer.invoke('nako:plugin:removeDesktop', payload),
  onWindowStateChanged: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('nako:window-state-changed', listener)

    return () => ipcRenderer.removeListener('nako:window-state-changed', listener)
  },
  onFocusSession: callback => {
    const listener = (_event, sessionId) => callback(sessionId)
    ipcRenderer.on('nako:focus-session', listener)

    return () => ipcRenderer.removeListener('nako:focus-session', listener)
  },
  onNotificationAction: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('nako:notification-action', listener)

    return () => ipcRenderer.removeListener('nako:notification-action', listener)
  },
  onNotificationActivate: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('nako:notification-activate', listener)

    return () => ipcRenderer.removeListener('nako:notification-activate', listener)
  },
  onExternalOpenFailed: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('nako:external-open-failed', listener)

    return () => ipcRenderer.removeListener('nako:external-open-failed', listener)
  },
  onPreviewFileChanged: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('nako:preview-file-changed', listener)

    return () => ipcRenderer.removeListener('nako:preview-file-changed', listener)
  },
  onBackendExit: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('nako:backend-exit', listener)

    return () => ipcRenderer.removeListener('nako:backend-exit', listener)
  },
  // Soft gateway-mode apply finished tearing down the primary backend. Renderer
  // should wipe session lists + re-dial without a window reload.
  onConnectionApplied: callback => {
    const listener = () => callback()
    ipcRenderer.on('nako:connection:applied', listener)

    return () => ipcRenderer.removeListener('nako:connection:applied', listener)
  },
  onPowerResume: callback => {
    const listener = () => callback()
    ipcRenderer.on('nako:power-resume', listener)

    return () => ipcRenderer.removeListener('nako:power-resume', listener)
  },
  // AC ↔ battery transitions; renderers slow their backstop polls on battery.
  getOnBattery: () => ipcRenderer.invoke('nako:power-battery:get'),
  onBatteryChanged: callback => {
    const listener = (_event, onBattery) => callback(Boolean(onBattery))
    ipcRenderer.on('nako:power-battery', listener)

    return () => ipcRenderer.removeListener('nako:power-battery', listener)
  },
  onBootProgress: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('nako:boot-progress', listener)

    return () => ipcRenderer.removeListener('nako:boot-progress', listener)
  },
  // First-launch bootstrap progress -- emitted by the install.ps1 stage
  // runner in main.ts (apps/desktop/electron/bootstrap-runner.ts).
  // Renderer's install overlay subscribes to live events and queries the
  // current snapshot via getBootstrapState() to recover after a devtools
  // reload mid-bootstrap.
  getBootstrapState: () => ipcRenderer.invoke('nako:bootstrap:get'),
  probeLocalBackend: () => ipcRenderer.invoke('nako:local-backend:probe'),
  continueBootstrapLocal: () => ipcRenderer.invoke('nako:bootstrap:continue-local'),
  recycleBackend: profile => ipcRenderer.invoke('nako:backend:recycle', profile),
  resetBootstrap: () => ipcRenderer.invoke('nako:bootstrap:reset'),
  updateHold: {
    recheck: () => ipcRenderer.invoke('nako:update-hold:recheck'),
    quit: () => ipcRenderer.invoke('nako:update-hold:quit'),
    startAnyway: (request: { holdId: string; confirmed: true }) =>
      ipcRenderer.invoke('nako:update-hold:start-anyway', request)
  },
  repairBootstrap: () => ipcRenderer.invoke('nako:bootstrap:repair'),
  cancelBootstrap: () => ipcRenderer.invoke('nako:bootstrap:cancel'),
  onBootstrapEvent: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('nako:bootstrap:event', listener)

    return () => ipcRenderer.removeListener('nako:bootstrap:event', listener)
  },
  getVersion: (scope?: { connectionId?: string; profile?: string }) => ipcRenderer.invoke('nako:version', scope),
  relaunchApp: () => ipcRenderer.invoke('nako:app:relaunch'),
  getMachineProfile: () => ipcRenderer.invoke('nako:machine:profile'),
  getRemoteDisplayReason: () => ipcRenderer.invoke('nako:get-remote-display-reason'),
  uninstall: {
    summary: () => ipcRenderer.invoke('nako:uninstall:summary'),
    run: mode => ipcRenderer.invoke('nako:uninstall:run', { mode }),
    openAppsSettings: () => ipcRenderer.invoke('nako:uninstall:openAppsSettings')
  },
  updates: {
    check: opts => ipcRenderer.invoke('nako:updates:check', opts),
    apply: opts => ipcRenderer.invoke('nako:updates:apply', opts),
    setChannel: name => ipcRenderer.invoke('nako:updates:channel:set', name),
    onProgress: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('nako:updates:progress', listener)

      return () => ipcRenderer.removeListener('nako:updates:progress', listener)
    },
    takePendingRun: () => ipcRenderer.invoke('nako:updates:metric:take'),
    ackPendingRun: sent => ipcRenderer.invoke('nako:updates:metric:ack', sent),
    onPendingRun: callback => {
      const listener = () => callback()
      ipcRenderer.on('nako:updates:metric:pending', listener)

      return () => ipcRenderer.removeListener('nako:updates:metric:pending', listener)
    }
  },
  desktopMetrics: {
    setEnabled: (on, profile) => ipcRenderer.invoke('nako:desktop-metrics:set-enabled', on, profile),
    takeRendererCrashes: () => ipcRenderer.invoke('nako:desktop-metrics:crash:take'),
    ackRendererCrashes: sent => ipcRenderer.invoke('nako:desktop-metrics:crash:ack', sent)
  },
  themes: {
    fetchMarketplace: id => ipcRenderer.invoke('nako:vscode-theme:fetch', id),
    searchMarketplace: query => ipcRenderer.invoke('nako:vscode-theme:search', query)
  },
  // Find-in-page (Ctrl/Cmd+F): delegates to Electron's
  // webContents.findInPage on the IPC sender's window so a Cmd+F pressed
  // in a secondary session window searches THAT window, not the primary.
  // `onFoundInPage` returns the unsubscribe fn; the renderer wires it via
  // `initFindInPageListener` in store/find-in-page.ts and tears it down
  // when the FindBar unmounts.
  findInPage: (query, options) => ipcRenderer.invoke('nako:find-in-page', query, options),
  stopFindInPage: () => ipcRenderer.invoke('nako:stop-find-in-page'),
  onFoundInPage: callback => {
    const listener = (_event, result) => callback(result)
    ipcRenderer.on('nako:found-in-page', listener)

    return () => ipcRenderer.removeListener('nako:found-in-page', listener)
  },
  // Main-process `before-input-event` forwards Ctrl/Cmd+F here so renderer
  // can open the FindBar even when the GTK compositor has already grabbed
  // the chord at the windowing layer (#81727).
  onOpenFindBarRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('nako:open-find-bar', listener)

    return () => ipcRenderer.removeListener('nako:open-find-bar', listener)
  }
})
