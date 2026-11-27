/**
 * Nako Cloud owns updates for its agents: it replaces the agent's image, so
 * no in-app updater may be offered there. Two shapes reach a Cloud agent:
 *
 * - the Webapp served BY a Cloud agent (the tab's own origin is the agent):
 *   every update target is Cloud's, because the app itself comes from it;
 * - native Desktop CONNECTED to a Cloud agent: only the backend target is;
 *   the Desktop app on this machine still updates itself.
 */

import { isNakoCloudUrl } from '@nako/shared'
import { computed } from 'nanostores'

import type { NakoConnection } from '@/global'
import { servedByNakoCloud } from '@/lib/nako-cloud-host'
import type { UpdateTarget } from '@/lib/update-copy'
import { $connection } from '@/store/session'

function connectedToNakoCloud(connection: NakoConnection | null): boolean {
  return connection?.mode === 'remote' && (connection.remoteKind === 'cloud' || isNakoCloudUrl(connection.baseUrl))
}

/** Whether Nako Cloud owns updates for `target` under `connection`. */
export function cloudOwnsUpdates(target: UpdateTarget, connection: NakoConnection | null = $connection.get()): boolean {
  return servedByNakoCloud() || (target === 'backend' && connectedToNakoCloud(connection))
}

/**
 * The target a generic "Update Nako" command acts on: the connected backend
 * in remote mode, unless Nako Cloud owns it — then this machine's own app.
 */
export function updateTargetFor(connection: NakoConnection | null): UpdateTarget {
  return connection?.mode === 'remote' && !cloudOwnsUpdates('backend', connection) ? 'backend' : 'client'
}

/** Per target: true while every update control for it must stay hidden. */
export const $cloudOwnsUpdates = computed($connection, connection => ({
  backend: cloudOwnsUpdates('backend', connection),
  client: cloudOwnsUpdates('client', connection)
}))
