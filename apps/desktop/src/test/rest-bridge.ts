import { vi } from 'vitest'

import type { NakoApiRequest } from '@/global'

/** Install a fake `window.nakoDesktop.api`, the preload bridge every REST helper sends through. */
export function installRestBridge(answer: (request: NakoApiRequest) => object) {
  const api = vi.fn(async (request: NakoApiRequest) => answer(request))

  Object.defineProperty(window, 'nakoDesktop', { configurable: true, value: { api } })

  return api
}
