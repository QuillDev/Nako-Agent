import { Codecs, persistentAtom } from '@/lib/persisted'

// One browsing layout across Skills and Plugins; Installed keeps its own list.
export const $catalogCardView = persistentAtom('nako.desktop.capabilities.catalogCardView', true, Codecs.bool)
