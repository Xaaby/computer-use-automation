# Design Report — Computer-Use Automation System

Opinionated engineering decisions for the interface.ai take-home.

## 1. Architecture

This is a **single-process, single-repo** system on purpose. The assignment is a vertical slice, not a multi-service platform. Four modules own clear seams:

1. **Discovery Agent** (`agent/`) — Bedrock LLM observe→decide→act; produces a trajectory.
2. **Capability schema/repository** (`capability/`) — the typed contract; Pydantic is source of truth.
3. **Replay Executor** (`replay/`) — deterministic execution of the contract; **no LLM**.
4. **Escalation Controller** (`escalation/`) — who owns the browser; asyncio.Event gate + FastAPI console.

**AWS Bedrock via boto3 `converse()`** is used instead of the Anthropic SDK. That matches enterprise deployment (IAM, regional endpoints, no second vendor SDK) while keeping the same tool-loop semantics.

The **`Surface` ABC** (`surfaces/base.py`) separates *what* the artifact says from *how* a concrete UI is driven. Replay only calls Surface methods. Today that is `PlaywrightWebSurface`; tomorrow it can be a desktop adapter without rewriting artifacts.

FastAPI (operator console + capability invoke API) runs with **`await server.serve()` in the same asyncio loop** as the executor. That is the only way `gate.set()` in an HTTP handler reliably wakes `gate.wait()` in the executor on Windows. `uvicorn.run()` and `reload=True` are forbidden here.

## 2. Artifact Schema

A capability artifact is a **contract**, not a click recording. It declares typed inputs/outputs, embedded policy, business outcomes, and an error taxonomy. Replay is validation of that contract.

Locators are an ordered candidate list (role+name → label → text → css → coords). Role+name is preferred because accessible names survive layout churn. Coords are last resort and documented as fragile. **Strict mode**: match count ≠ 1 is always a hard failure — never pick one of many.

**Business outcomes** (e.g. `MEMBER_NOT_FOUND`) live in the artifact. The caller asked a question and got a valid answer; that is not an exception path.

We keep **schema_version** (wire format of the artifact language) separate from **capability version** (semver of this procedure). Provenance starts as `status: "draft"` until a successful replay earns trust.

Ephemeral ARIA refs (`e17`) are **never** stored. Discovery resolves them live; the compiler stores durable role/name/label candidates (and `frame_path` for iframes).

## 3. Determinism & Error Handling

Terminal outcomes are five typed classes, not a boolean:

- `ReplaySuccess`
- `BusinessOutcome`
- `HardFailure`
- `RecoverableExhausted`
- **`IndeterminateCommit`**

`IndeterminateCommit` exists because a timed-out irreversible action (confirm transfer) may or may not have posted. **Retrying can double a bank transaction.** The executor never retries this class; it escalates for human reconciliation.

Checkpoints track **state transitions** (search page → member detail), not every keystroke. Session recovery is a **declared** `re_authenticate` routine using env credentials — not a catch-all exception handler.

Locator ambiguity is always `AMBIGUOUS_LOCATOR` hard failure. Guessing the wrong "Search" button is worse than stopping.

**MEMBER_NOT_FOUND ID decision:** the mock app maps member IDs starting with `9` to HTTP 403 (`PERMISSION_DENIED`). Docs that used `99999` for not-found would mis-classify. We use **`88888`** for not-found and **`90001`** for permission denied.

## 4. Heterogeneity & Multi-Tenant

`PlaywrightWebSurface` covers modern and legacy web (tables, iframes, no test IDs). Iframe content is in the discovery ARIA snapshot via `mode="ai"`; artifacts still store `frame_path` (e.g. `iframe[title="Accounts"]`) so replay does not depend on ephemeral refs.

A future `WindowsUISurface` would implement the same ABC with UI Automation. Artifacts stay logical.

Multi-tenant overrides (`base capability + tenant_overrides.json`) are designed as a seam, not built. Drift detection would fingerprint ARIA structure at checkpoints — also a cut.

## 5. Escalation & Handoff

Escalation keeps the **same Playwright Page** open. The human operates that window; we do not spawn a second browser or hand them a stale screenshot as the only control surface.

The **asyncio.Event gate** pauses the executor coroutine without blocking the event loop. FastAPI handlers call `resume()` / `abort()` which set the gate.

Human actions are captured with **`context.add_init_script` + `expose_binding`** (D6). The script never reads `element.value` — only field identity. Server-side `redact_log_entry` is a second layer.

Post-resume, automation continues at the next step after the gate opens; irreversible steps additionally require an **approval token** from `POST /accept`.

What is mocked: the operator UI is minimal HTML. A product console would stream the session (WebRTC) into a richer ops UI.

## 6. Safety

Policy is enforced **inside** `Surface.resolve_and_act` / `navigate` **before** any browser action — below both the LLM and the replay planner. Allowlist config (`policy/allowlist.json`) is checked every time, not only at plan time.

Irreversible commits require an approval token. PII is redacted on all log writes; screenshots can mask labeled fields. Credentials live only in environment variables — never in artifacts or evidence JSON (screenshots stay out of intervention files on disk).

## 7. Cuts

For each cut: what it is, the seam that remains, what would change, why it is the right next step.

1. **Remote co-browsing operator console (WebRTC)** — Seam: EscalationController + same Page object. Change: stream viewport to a hosted console. Right next: highest ops UX payoff once the gate works.
2. **Desktop surface adapter** — Seam: `Surface` ABC. Change: Windows UI Automation implementation. Right next: proves heterogeneity without rewriting artifacts.
3. **Multi-tenant at scale** — Seam: artifact name + optional override layer. Change: sparse override store and merge at load time. Right next after single-tenant stability is proven.
4. **draft → approved gate with confidence scoring** — Seam: `provenance.status`. Change: require N successful replays + human approve. Right next for production promotion.
5. **Assisted LLM fallback on single-step replay failure** — Seam: ReplayExecutor hard_failure path. Change: bounded one-step rediscovery then recompile candidate. Right next only after strict replay is measurably stable (otherwise it hides flaky locators).
