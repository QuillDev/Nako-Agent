/**
 * Which persisted keys are machine-level UI preferences (#145213).
 *
 * Only these leave localStorage for the machine's shared store, so native
 * Desktop and every browser that opens this machine's Webapp show the same
 * choices. Scoped families keep their scope inside the key name (a
 * `….profile.<name>` or per-connection suffix): only the location changes.
 *
 * Deliberately NOT here (they stay in this browser/window):
 * - credentials, grants and connection config: gateway registry, custom model
 *   endpoints, plugin decisions and preview-script grants, embed permissions;
 * - per-window or per-device runtime state: layout tree, open tiles and tabs,
 *   last route/session, scroll, zoom, pet and popout geometry, keep-awake;
 * - drafts, queues, journals and caches: composer queue, in-flight turns,
 *   pending/prepared submissions, previews, pricing, backend skin cache, the
 *   "provider configured" onboarding cache (each backend answers that itself);
 * - one-off notices: butterbar, update and real-profile prompts.
 */

const SHARED_KEYS: ReadonlySet<string> = new Set([
  // Appearance.
  'nako-desktop-theme-v2',
  'nako-desktop-mode-v1',
  'nako-desktop-profile-themes-v1',
  'nako-desktop-profile-modes-v1',
  'nako-desktop-user-themes-v1',
  'nako.desktop.backdrop.v1',
  'nako.desktop.chat-text-scale.v1',
  'nako.desktop.interfaceMode.v1',
  'nako.desktop.textDirection',
  'nako.desktop.translucency.v2',
  'nako.desktop.user-bubble-transparency.v1',
  // Sessions sidebar and profile rail.
  'nako.desktop.agentsGroupedByWorkspace',
  'nako.desktop.profileColors',
  'nako.desktop.profileOrder',
  'nako.desktop.profileRailVisible',
  'nako.desktop.sessionListDensity',
  'nako.desktop.sessionOrder',
  'nako.desktop.sessionOrder.manual',
  'nako.desktop.showAllProfiles',
  'nako.desktop.sidebarAgentsGrouped.allProfiles',
  'nako.desktop.sidebarCardRows',
  'nako.desktop.sidebarGrouping',
  'nako.desktop.sidebarGrouping.allProfiles',
  'nako.desktop.sidebarRowMeta',
  'nako.desktop.sidebarShowAllSessions',
  'nako.desktop.sidebarShowArchived',
  'nako.desktop.sidebarSortKey',
  // Status bar.
  'nako.desktop.statusbarHidden.v2',
  'nako.desktop.statusbarVisible.v2',
  // Transcript and tools.
  'nako.desktop.hideThreadTimeline',
  'nako.desktop.reasoning.collapsedByDefault',
  'nako.desktop.toolView.hideCodeDiffs',
  'nako.desktop.toolView.technical',
  // Shell, panes and behaviour.
  'nako.desktop.alwaysExternalLinks.v1',
  'nako.desktop.capabilities.catalogCardView',
  'nako.desktop.composerPopout.enabled',
  'nako.desktop.composerPopout.gesturesEnabled',
  'nako.desktop.files.showIgnored',
  'nako.desktop.keybinds',
  'nako.desktop.layoutPresets.v2',
  'nako.desktop.panesFlipped',
  'nako.desktop.tabStripDefault',
  'nako.desktop.titlebarAppActions',
  // Guidance: what the user already skipped, saw or dismissed does not come
  // back in every new browser or origin (#145600).
  'nako-onboarding-skipped-v1',
  'nako.desktop.tips.last.v1',
  'nako.desktop.tips.next.v1',
  'nako.desktop.tips.retired.v1',
  'nako.desktop.tips.rotation.v1',
  'nako.desktop.tips.shownAt.v1',
  'nako.desktop.tours.v1',
  // Sound and media.
  'nako.desktop.autoSpeakReplies',
  'nako.desktop.completionSoundVariantId',
  'nako.desktop.hapticsMuted',
  'nako.desktop.videoPlaybackSpeed',
  // Model picker.
  'nako.desktop.collapsed-providers',
  'nako.desktop.favorite-models',
  // visible-models is a diff against known-models: they only make sense together.
  'nako.desktop.known-models',
  'nako.desktop.model-presets',
  'nako.desktop.visible-models'
])

/** Families whose scope rides in the key suffix (gateway aliases/order/collapsed). */
const SHARED_PREFIXES: readonly string[] = ['nako.desktop.sidebar.gatewayGroups.v1.']

export function isSharedPreferenceKey(key: string): boolean {
  return SHARED_KEYS.has(key) || SHARED_PREFIXES.some(prefix => key.startsWith(prefix))
}
