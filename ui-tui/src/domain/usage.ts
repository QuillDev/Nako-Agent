import type { Usage } from '@nako/shared/gateway-events'

export { usageFromTick } from '@nako/shared/usage-tick'

export const ZERO: Usage = { calls: 0, input: 0, output: 0, total: 0 }
