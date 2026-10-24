import { describe, expect, it } from 'vitest'
import { SseParser } from '@/lib/sse'

describe('SseParser', () => {
  it('parses id, event and data fields', () => {
    const p = new SseParser()
    const frames = p.push('id: 42\nevent: phase\ndata: {"id":42}\n\n')
    expect(frames).toEqual([{ id: '42', event: 'phase', data: '{"id":42}' }])
  })

  it('joins multi-line data and ignores comments/heartbeats', () => {
    const p = new SseParser()
    const frames = p.push(': ping\n\ndata: line1\ndata: line2\n\n')
    expect(frames).toEqual([{ id: null, event: null, data: 'line1\nline2' }])
  })

  it('handles frames split across chunks, including a CRLF split', () => {
    const p = new SseParser()
    expect(p.push('id: 7\r')).toEqual([])
    expect(p.push('\ndata: {"a"')).toEqual([])
    expect(p.push(':1}\r\n\r\n')).toEqual([{ id: '7', event: null, data: '{"a":1}' }])
  })

  it('does not carry the id of one frame into the next (ephemeral events have no id)', () => {
    const p = new SseParser()
    const frames = p.push('id: 1\ndata: a\n\ndata: b\n\n')
    expect(frames.map((f) => f.id)).toEqual(['1', null])
  })
})
