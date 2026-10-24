import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

afterEach(() => {
  cleanup()
  window.localStorage.clear()
})

// jsdom lacks a few browser APIs used by Radix primitives and layout code.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver
window.matchMedia ??= ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener: () => {},
  removeListener: () => {},
  addEventListener: () => {},
  removeEventListener: () => {},
  dispatchEvent: () => false,
})) as typeof window.matchMedia
Element.prototype.scrollIntoView ??= () => {}
Element.prototype.hasPointerCapture ??= () => false
Element.prototype.releasePointerCapture ??= () => {}

// Monaco can't run in jsdom; components that need it are never asserted on its internals.
vi.mock('@/lib/monaco', () => ({ monaco: {}, EDITOR_FONT: 'monospace', monacoTheme: () => 'vs-dark' }))
vi.mock('@monaco-editor/react', () => ({
  default: () => null,
  DiffEditor: () => null,
  loader: { config: () => {} },
}))
