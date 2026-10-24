import { describe, expect, it } from 'vitest'
import { parseUnifiedDiff } from '@/lib/diff'
import { SAMPLE_DIFF } from './fixtures'

describe('parseUnifiedDiff', () => {
  it('parses modified and created files with line numbers and counts', () => {
    const files = parseUnifiedDiff(SAMPLE_DIFF)
    expect(files).toHaveLength(2)
    const [mod, created] = files
    expect(mod.path).toBe('offers/merge.py')
    expect(mod.change).toBe('modify')
    expect(mod.additions).toBe(2)
    expect(mod.deletions).toBe(2)
    const first = mod.hunks[0].lines[0]
    expect(first).toMatchObject({ type: 'context', oldNo: 9, newNo: 9 })
    const del = mod.hunks[0].lines.find((l) => l.type === 'del')
    expect(del).toMatchObject({ oldNo: 10, newNo: null })
    expect(created.path).toBe('tests/test_merge_regression.py')
    expect(created.change).toBe('create')
    expect(created.additions).toBe(3)
  })

  it('parses git-style headers', () => {
    const files = parseUnifiedDiff(
      'diff --git a/x.txt b/x.txt\nnew file mode 100644\n--- /dev/null\n+++ b/x.txt\n@@ -0,0 +1 @@\n+hi\n',
    )
    expect(files).toHaveLength(1)
    expect(files[0]).toMatchObject({ path: 'x.txt', change: 'create', additions: 1 })
  })

  it('returns nothing for empty input', () => {
    expect(parseUnifiedDiff('')).toEqual([])
  })
})
