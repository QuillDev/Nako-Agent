import { describe, expect, it } from 'vitest'

import { isNakoCloudUrl } from './nako-cloud'

describe('isNakoCloudUrl', () => {
  it('recognises production and staging agent hosts, nothing else', () => {
    expect(isNakoCloudUrl('https://ares-3009.agents.nousresearch.com')).toBe(true)
    expect(isNakoCloudUrl('https://ares--app.agents.staging-nousresearch.com/')).toBe(true)
    expect(isNakoCloudUrl('https://agents.nousresearch.com.evil.example')).toBe(false)
    expect(isNakoCloudUrl('https://my-box.tailnet.ts.net')).toBe(false)
    expect(isNakoCloudUrl('not a url')).toBe(false)
    expect(isNakoCloudUrl(undefined)).toBe(false)
  })
})
