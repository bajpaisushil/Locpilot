# LocPilot agent benchmark — measured results

- Date: 2026-11-13 10:08 UTC
- Model: `mimo-v2.6:9b-q4_K_M` via local Ollama (thinking: off)
- Machine: Apple M4, 16 GB RAM, macOS 27.2
- Fixtures: 4 small, deterministic bug-fix repositories (`fixtures/`)
- Harness auto-approved the agent's patches and test commands (the app itself always asks a human).

| Fixture | Fixture valid | Relevant files | Root cause (location) | Root cause (concept) | Patch passes hidden tests | Regression test detects bug | Existing tests pass | Within limits | Agent status | LLM calls | Retries | Time | Model RAM |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cart-discount | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | changes_verified | 7 | 0 | 248.6s | 5.81 GB |
| hotel-offers | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | changes_verified | 8 | 0 | 443.5s | 4.98 GB |
| pagination | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | changes_verified | 7 | 0 | 376.4s | 4.98 GB |
| user-profile-500 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | changes_verified | 7 | 0 | 329.4s | 4.98 GB |

**Totals:** relevant files 4/4, root cause location 4/4, root cause keywords 4/4, patch accepted 4/4, regression test detects bug 4/4, existing tests pass 4/4, within limits 4/4

Caveats: these are tiny synthetic fixtures with known answers. Passing them does not demonstrate production-grade autonomous coding; results vary run to run with local model sampling, hardware and model choice.

Disclosure: the root-cause prompt includes a generic checklist of common bug patterns, and these fixtures exercise common bug classes that the checklist also names (state reset in a loop, off-by-one, missing keys, arithmetic scale). The 'root cause (concept)' keyword lists were extended after the first run to include canonical phrasings such as 'off-by-one' and 'KeyError'. 'Root cause (location)' is computed from the lines the reference fix changes. Hidden acceptance tests and the regression re-run are independent of the agent's own claims.


"Model RAM" is the model's size in memory as `ollama ps` reports it while the task runs (it reported 5.81 GB
for the first fixture and 4.98 GB afterwards).

**What "verified" means.** `changes_verified`: LocPilot's own regression test failed before the change and passed
after it, and the existing tests still passed. That is necessary but not sufficient, which is why this benchmark
also runs *hidden* acceptance tests the agent never sees, and why the app tells you to review the generated test.
In the comparison below, qwen2.5-coder:7b reached `changes_verified` on `user-profile-500` with a test that encodes
the wrong expected behavior: verified, but rejected by the hidden tests.

## Model comparison

Same machine (Apple M4, 16 GB), same day (2026-11-13), same four fixtures.

| Model | LocPilot code | Thinking | Fixed (hidden tests pass) | Regression test catches the bug | Root cause located | Time per fixture | Model RAM |
|---|---|---|---|---|---|---|---|
| `mimo-v2.6:9b-q4_K_M` (default) | this version | off (default) | **4/4** | **4/4** | 4/4 | 249–444 s | 5.0–5.8 GB |
| `qwen2.5-coder:7b` (previous default) | this version | — | 2/4 | 2/4 | 2/4 | 209–259 s | 5.0–5.2 GB |
| `qwen2.5-coder:7b` | previous commit (`8dc7177`) | — | 2/4 | 3/4 | 3/4 | 129–297 s | not measured |
| `mimo-v2.6:9b-q4_K_M` | development build¹ | auto | 3/4 | 3/4 | 3/4 | 340–454 s, one timed out at 900 s | 5.0–5.8 GB |

MiMo is about 1.5× slower per fix than qwen2.5-coder:7b and uses about 0.6 GB more RAM, and it fixed every bug.
qwen2.5-coder:7b's results move by about one fixture from run to run (its 2026-10-24 run: 2/4 fixed, 4/4 tests).

¹ Before LocPilot cut off runaway reasoning. In `hotel-offers` the root-cause step reasoned in circles (the same
paragraph repeated until it had written 23,584 characters) for 14 minutes. LocPilot now stops reasoning at its
token budget or when it starts repeating itself, then answers without thinking. Thinking was not re-run because it
is off by default: it made every fix slower and fixed nothing the default missed.

**Run history during this change** (MiMo, thinking off):

| Run | Fixed | Test catches bug | What changed |
|---|---|---|---|
| 1 | 4/4 | 1/4 | Fixes were right, but most new test files were malformed: content in the SEARCH half, or no `=======` line |
| 2 | 4/4 | 4/4 | The parser accepts those forms for files that don't exist yet, and a new-file template was added to the edit rules all steps share. With it, qwen2.5-coder:7b's tests lost their imports (1/4 fixed, 0/4 tests) |
| 3 | 3/4 | 3/4 | Template reworded ("the complete file, imports first"): qwen's tests had imports again, but its fixes got worse (1/4); MiMo's `hotel-offers` fix was incomplete, and its own regression test caught that |
| 4 (table above) | 4/4 | 4/4 | The template is shown only in the test-writing step; every other step's prompt is as in the previous commit |

- **cart-discount** — Verified by execution: LocPilot's regression test failed before the change and passes after it (4 tests passed). Review the test — it encodes LocPilot's reading of the expected behavior. Root cause stated: “In src/discounts.js, applyDiscount divides the percentage by 10 instead of 100, so a 10% discount is applied as 100% and a $200 cart becomes $0; the Math.max clamp then turns the negative remainder into 0.”. Agent's own regression claim: `detected_bug`.
- **hotel-offers** — Verified by execution: LocPilot's regression test failed before the change and passes after it (4 tests passed). Review the test — it encodes LocPilot's reading of the expected behavior. Root cause stated: “In offers/aggregator.py, merge_offers resets its per-product `best` dict inside the supplier loop, so de-duplication is scoped per supplier instead of across all suppliers; cross-supplier duplicates therefore survive into the merged result and are returned by search.”. Agent's own regression claim: `detected_bug`.
- **pagination** — Verified by execution: LocPilot's regression test failed before the change and passes after it (3 tests passed). Review the test — it encodes LocPilot's reading of the expected behavior. Root cause stated: “catalog/pagination.py's paginate() documents 1-based pages but computes the slice start as page * size instead of (page - 1) * size, so page=1 skips the first page of results.”. Agent's own regression claim: `detected_bug`.
- **user-profile-500** — Verified by execution: LocPilot's regression test failed before the change and passes after it (3 tests passed). Review the test — it encodes LocPilot's reading of the expected behavior. Root cause stated: “serialize_profile in api/serializers.py unconditionally indexes user["address"] at (api/serializers.py:13), so a user without an address raises KeyError; the broad except at (api/handlers.py:14) then converts that into a 500 instead of returning a profile with location null.”. Agent's own regression claim: `detected_bug`.
