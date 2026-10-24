# LocPilot UI (React + Tauri)

The desktop UI for LocPilot. It runs in the Tauri webview (desktop app) or in a browser during
development, and talks only to the local LocPilot backend described in [`../docs/api.md`](../docs/api.md).

| Path | What |
|---|---|
| `src/lib/api.ts`, `src/lib/types.ts` | Typed client for the backend contract (`docs/api.md`) |
| `src/lib/sse.ts`, `src/hooks/useTaskStream.ts` | fetch-based SSE reader (sends the bearer token), replay with `after=`, reconnect |
| `src/lib/timeline.ts` | Folds persisted task events into per-turn timelines |
| `src/components/setup/` | Backend startup gate and first-run dependency setup (Ollama, model, Git) |
| `src/components/chat/` | Conversation, timeline, approval card, RCA/verification report |
| `src/components/panels/` | Files (explorer + Monaco), Changes (Monaco diff), Commands, Details |
| `src-tauri/` | Tauri v2 shell: sidecar lifecycle (`src/backend.rs`), capabilities, CSP, icons |

```bash
npm install
npm run dev          # browser mode on http://127.0.0.1:5420 (backend expected on 127.0.0.1:8420)
npm run typecheck && npm run lint && npm test
npm run tauri dev    # desktop dev mode (build the sidecar first: python3 ../scripts/build_sidecar.py)
```

See [`../docs/desktop.md`](../docs/desktop.md) for the desktop architecture, packaging and releases.
