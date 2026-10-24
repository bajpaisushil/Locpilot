# LocPilot agent benchmark — measured results

- Date: 2026-10-24 15:20 UTC
- Model: `qwen2.5-coder:7b` via local Ollama
- Machine: Apple M4, 16 GB RAM, macOS 26.5
- Fixtures: 4 small, deterministic bug-fix repositories (`fixtures/`)
- Harness auto-approved the agent's patches and test commands (the app itself always asks a human).

| Fixture | Fixture valid | Relevant files | Root cause (location) | Root cause (concept) | Patch passes hidden tests | Regression test detects bug | Existing tests pass | Within limits | Agent status | LLM calls | Retries | Time |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cart-discount | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | changes_verified | 7 | 0 | 365.5s |
| hotel-offers | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | changes_verified | 7 | 0 | 159.9s |
| pagination | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | changes_verified | 8 | 0 | 171.4s |
| user-profile-500 | ✅ | ✅ | ❌ | ✅ | ❌ | ✅ | ✅ | ✅ | changes_verified | 7 | 0 | 159.4s |

**Totals:** relevant files 4/4, root cause location 2/4, root cause keywords 3/4, patch accepted 2/4, regression test detects bug 4/4, existing tests pass 4/4, within limits 4/4

Caveats: these are tiny synthetic fixtures with known answers. Passing them does not demonstrate production-grade autonomous coding; results vary run to run with local model sampling, hardware and model choice.

**What "verified" means.** Every run above ended in `changes_verified`: LocPilot's own regression test
failed before the change and passed after it, and existing tests still passed. The *hidden* acceptance
tests show that this is necessary but not sufficient — for `hotel-offers` and `user-profile-500` the agent's
test encoded its own (wrong) reading of the expected behavior. That is why the app tells you to review the
generated test, and why this benchmark measures acceptance independently.

Disclosure: the root-cause prompt includes a generic checklist of common bug patterns, and these fixtures
exercise common bug classes that the checklist also names (state reset in a loop, off-by-one, missing keys,
arithmetic scale). The "root cause (concept)" keyword lists were extended after the first run to include
canonical phrasings such as "off-by-one" and "KeyError". "Root cause (location)" is computed from the lines the
reference fix changes. Hidden acceptance tests and the regression re-run are independent of the agent's claims.

**Run history during development (same machine and model):**

| Run | Patch passes hidden tests | Notes |
|---|---|---|
| 1 | 2/4 | `cart-discount` stopped after the analysis (request classified as "root cause only") |
| 2 | 0/4 | exposed three LocPilot bugs, all fixed: Node tests blocked by the macOS sandbox profile, the model could veto a repair retry, and a failed repair could mask an applied patch in the final status |
| 3 (this table) | 2/4 | `hotel-offers` was fixed correctly in runs 1 and in manual runs, but not here — local-model results vary from run to run |

- **cart-discount** — Verified by execution: LocPilot's regression test failed before the change and passes after it (4 tests passed). Review the test — it encodes LocPilot's reading of the expected behavior. Root cause stated: “The discount calculation in `applyDiscount` is incorrect. The discount should be applied as a percentage, not as a full amount.”. Agent's own regression claim: `detected_bug`.
- **hotel-offers** — Verified by execution: LocPilot's regression test failed before the change and passes after it (4 tests passed). Review the test — it encodes LocPilot's reading of the expected behavior. Root cause stated: “The dedupe_key method is not correctly identifying duplicate offers.”. Agent's own regression claim: `detected_bug`.
- **pagination** — Verified by execution: LocPilot's regression test failed before the change and passes after it (3 tests passed). Review the test — it encodes LocPilot's reading of the expected behavior. Root cause stated: “Off-by-one error in start index calculation”. Agent's own regression claim: `detected_bug`.
- **user-profile-500** — Verified by execution: LocPilot's regression test failed before the change and passes after it (3 tests passed). Review the test — it encodes LocPilot's reading of the expected behavior. Root cause stated: “The address dictionary is None for user 3, causing a KeyError when trying to access its 'city' and 'country' keys.”. Agent's own regression claim: `detected_bug`.
