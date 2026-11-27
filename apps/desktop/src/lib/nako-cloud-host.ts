/**
 * The Webapp tab is served BY a Nako Cloud agent (its own origin is the
 * agent). Cloud then owns every update: the app itself comes from that agent.
 * Kept free of the session store so low-level modules (notifications, the
 * settings manifest) can ask it without an import cycle.
 */

import { isNakoCloudUrl } from '@nako/shared'

import { isBrowserHostedDesktop } from '@/lib/platform'

export function servedByNakoCloud(): boolean {
  return typeof window !== 'undefined' && isBrowserHostedDesktop() && isNakoCloudUrl(window.location.origin)
}
