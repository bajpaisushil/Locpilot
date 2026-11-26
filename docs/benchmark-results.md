# LocPilot agent benchmark — measured results

- Date: 2026-11-26 10:32 UTC
- Model: `mimo-v2.6:9b-q4_K_M` via local Ollama (thinking: off)
- Machine: Apple M4, 16 GB RAM, macOS 27.2
- Fixtures: 4 small, deterministic bug-fix repositories (`fixtures/`)
- Harness auto-approved the agent's patches and test commands (the app itself always asks a human).

| Fixture | Fixture valid | Relevant files | Root cause (location) | Root cause (concept) | Patch passes hidden tests | Regression test detects bug | Existing tests pass | Within limits | Agent status | LLM calls | Retries | Time | Model RAM |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| hotel-offers | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | changes_verified | 16 | 0 | 698.5s | 6.18 GB |
| cart-discount | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | changes_verified | 14 | 0 | 262.0s | 6.18 GB |
| pagination | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | changes_verified | 18 | 0 | 359.3s | 6.18 GB |
| user-profile-500 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | changes_verified | 24 | 0 | 368.4s | 6.18 GB |

**Totals:** relevant files 4/4, root cause location 4/4, root cause keywords 4/4, patch accepted 4/4, regression test detects bug 4/4, existing tests pass 4/4, within limits 4/4

Caveats: these are tiny synthetic fixtures with known answers. Passing them does not demonstrate production-grade autonomous coding; results vary run to run with local model sampling, hardware and model choice.

Disclosure: the fix step's self-check (the model walks the request's example through its change) was developed on `hotel-offers`, where the model's first fix was a no-op three runs in a row; its wording names the bug class of that fixture (a statement inside a loop that belongs after it). Also: the root-cause prompt includes a generic checklist of common bug patterns, and these fixtures exercise common bug classes that the checklist also names (state reset in a loop, off-by-one, missing keys, arithmetic scale). The 'root cause (concept)' keyword lists were extended after the first run to include canonical phrasings such as 'off-by-one' and 'KeyError'. 'Root cause (location)' is computed from the lines the reference fix changes. Hidden acceptance tests and the regression re-run are independent of the agent's own claims.


"Model RAM" is the model's size in memory as `ollama ps` reports it while the task runs (6.18 GB with the 32k
context window LocPilot now chooses for this model; 5.0–5.8 GB with the 16k window of the runs below).

**What "verified" means.** `changes_verified`: LocPilot's own regression test failed before the change and passed
after it, and the existing tests still passed. That is necessary but not sufficient, which is why this benchmark
also runs *hidden* acceptance tests the agent never sees, and why the app tells you to review the generated test.
In the comparison below, qwen2.5-coder:7b reached `changes_verified` on `user-profile-500` with a test that encodes
the wrong expected behavior: verified, but rejected by the hidden tests.

## Compared with Claude Code on a real repository

The fixtures above are tiny. This comparison uses a real TypeScript monorepo (212 files, 437 vitest tests) and
eleven tasks of the kinds people actually ask for. Each task starts from a fresh Git copy; both agents get the same
request text and nothing else.

- Machine: Apple M4, 16 GB. LocPilot: `mimo-v2.6:9b-q4_K_M` on local Ollama, thinking off, 32k context (chosen
  automatically). Claude Code: its cloud model, same repository copies.
- Judged independently of what the agent claims: **hidden acceptance tests** the agents never see, the project's
  whole test suite, and whether the agent's own new test fails on the original code. "Explain" is judged on five
  required facts, "write tests" by mutation score (six planted mutants).
- One run per task; LocPilot's harness auto-approves (the app always asks) and stops a task after 25 minutes.

| Task | Claude Code | LocPilot before this change¹ | LocPilot now |
|---|---|---|---|
| Bug, suite already red (Raft vote) | ✅ 72 s | ✅ 650 s (not verified by a test) | ✅ 560 s |
| Bug (equal jitter) | ✅ 68 s | ✅ 418 s | ✅ 951 s |
| Bug (circuit breaker) | ✅ 82 s | ❌ fix right, own test wrong, suite left red | ✅ 942 s |
| Bug (replica duplicate) | ✅ 60 s | ❌ no change made | ✅ 1094 s |
| Bug (percentile sort) | ✅ 63 s | ❌ no change made | ❌ 1417 s: wrong cause (the model does not notice a `sort()` without a compare function), reported as "verification failed" |
| Feature across two packages (linear backoff) | ✅ 204 s | ❌ feature right, suite left red | ✅ 966 s |
| Feature (p90 in the histogram) | ✅ 103 s | ❌ no change made | ✅ 1322 s |
| UI change (run clock shows hours) | ✅ 134 s | ❌ "verified", but wrong | not measured on this version |
| New algorithm in a new file (token bucket) | ✅ 140 s | ❌ wrong, suite left red | ❌ 1003 s: "verified" by its own test, but 2 of 6 hidden tests fail (milliseconds and seconds mixed up) |
| Explain code (load balancing) | ✅ 5/5 facts, 24 s | 4/5 facts, 200 s | ✅ 5/5 facts, 337 s |
| Write tests (lock table) | ✅ 6/6 mutants, 97 s | ❌ tests did not pass | not measured on this version |
| **Clean passes** | **11/11** | **2/11** | **7 of 9 measured** |

¹ The text protocol (SEARCH/REPLACE blocks), measured on 2026-11-24 before the work described below.

**What changed between the two LocPilot columns.** The model was not the problem in most failures: MiMo is tuned
to work by calling tools, and it answered edit steps with correct `edit` calls that LocPilot never read ("no
SEARCH/REPLACE blocks found"). LocPilot now offers tool-capable models real tools for exploring and editing, keeps
steps in one conversation so the model server can reuse its work, checks a change against the request before it
is proposed, and attributes failing tests (its own new test, existing tests, tests that were already red) before
deciding whether the code or the test must change. See [architecture.md](architecture.md).

**What the comparison does not show.**

- It is a development set, not a held-out one: the tasks, the hidden tests and the fixes to LocPilot come from the
  same work, and every failure seen on these tasks was used to improve LocPilot. Expect less on other repositories.
- One run per task. The local model samples, and results move from run to run.
- The "now" column was measured while LocPilot was still being changed: the first three bug tasks before the
  last speed changes (code shown once, stopping when the model only re-checks its work), and all of them before
  the fix step's walk-through of the request's example was added. None was run again on the final version; the
  four fixtures above were.
- Two tasks (UI change, write tests) have no measurement on this version: their only runs were cut short when the
  machine went to sleep, and the write-tests run exposed a bug that has since been fixed (a test file longer than
  one reply was cut off and lost).
- Speed: LocPilot needs 6–22 minutes per task on this machine, Claude Code 1–3.5.
- A "verified" from LocPilot means its own test failed before the change and passes after it. The token-bucket
  task shows the limit of that: code and test shared one misunderstanding. Review the generated test.

## Model comparison

Same machine (Apple M4, 16 GB), same day (2026-11-13), same four fixtures — an earlier version of LocPilot (commit `3491ed5`, text protocol), kept for the comparison between models.

| Model | LocPilot code | Thinking | Fixed (hidden tests pass) | Regression test catches the bug | Root cause located | Time per fixture | Model RAM |
|---|---|---|---|---|---|---|---|
| `mimo-v2.6:9b-q4_K_M` (default) | `3491ed5` | off (default) | **4/4** | **4/4** | 4/4 | 249–444 s | 5.0–5.8 GB |
| `qwen2.5-coder:7b` (previous default) | `3491ed5` | — | 2/4 | 2/4 | 2/4 | 209–259 s | 5.0–5.2 GB |
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

- **hotel-offers** — Verified by execution: LocPilot's regression test failed before the change and passes after it (4 tests passed). Review the test — it encodes LocPilot's reading of the expected behavior. Root cause stated: “In `offers/aggregator.py`, `merge_offers` reinitialises the `best` dict for each supplier (offers/aggregator.py:14), so the cheapest-offer-per-product comparison only happens within a single supplier; moving that dict outside the supplier loop (offers/aggregator.py:13) makes it accumulate across suppliers and keeps only the cheapest offer per product overall.”. Agent's own regression claim: `detected_bug`.
- **cart-discount** — Verified by execution: LocPilot's regression test failed before the change and passes after it (4 tests passed). Review the test — it encodes LocPilot's reading of the expected behavior. Root cause stated: “In src/discounts.js, applyDiscount divides the amount by 10 instead of 100, treating the percentage as tenths rather than hundredths; combined with the Math.max clamp, a 10% discount zeroes out the cart total.”. Agent's own regression claim: `detected_bug`.
- **pagination** — Verified by execution: LocPilot's regression test failed before the change and passes after it (3 tests passed). Review the test — it encodes LocPilot's reading of the expected behavior. Root cause stated: “In catalog/pagination.py, paginate() uses start = page * size, treating the 1-based page number as a 0-based list index; changing it to (page - 1) * size makes page=1 return items 1-10.”. Agent's own regression claim: `detected_bug`.
- **user-profile-500** — Verified by execution: LocPilot's regression test failed before the change and passes after it (3 tests passed). Review the test — it encodes LocPilot's reading of the expected behavior. Root cause stated: “serialize_profile in api/serializers.py unconditionally indexes user['address'] and calls serialize_address on it, so a user record without an address raises a KeyError inside the handler's try block and is converted to a 500 by the broad except Exception in get_user_profile; the fix is to make serialize_address return null when the address is missing so the profile's location is null.”. Agent's own regression claim: `detected_bug`.
