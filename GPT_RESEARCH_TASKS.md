# GPT Research Tasks — Status Update

## COMPLETED — No Further Research Needed

D1 — ARIA snapshot behavior: CONFIRMED. `mode="ai"` includes iframes + refs. Unlabeled cells use text as name.
D2 — Playwright asyncio Windows: CONFIRMED. ProactorEventLoop. Use `asyncio.run(main())` at entrypoint.
D3 — FastAPI + asyncio.Event: CONFIRMED. Use `asyncio.create_task(server.serve())` in same loop.
D4 — Playwright tracing: CONFIRMED. `context.tracing.start(screenshots=True, snapshots=True, sources=True)`. Viewer: `playwright show-trace trace.zip`. Keep paths shallow on Windows.
D5 — Claude tool use loop: CONFIRMED (using Bedrock format — see PHASE_3_INSTRUCTIONS.md for exact boto3 format).
D6 — add_init_script + expose_binding: CONFIRMED. Persists across navigation. Never capture element.value.
D7 — Ref → durable locator: CONFIRMED. Use `page.locator("aria-ref=eN").normalize()`. Never store refs in artifacts.

All research tasks D1-D7 are resolved. Cursor can now implement all phases without blockers.

---

## ONE Remaining Experiment (Optional but Recommended Before Phase 3)

Run this quick validation before Cursor starts Phase 3 to confirm aria-ref locator works on your machine:

```python
import asyncio, sys
asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=False)
        ctx = await b.new_context()
        page = await ctx.new_page()
        await page.goto("http://localhost:5000/members/search")
        
        # Get snapshot with refs
        snap = await page.aria_snapshot(mode="ai")
        print("Snapshot (first 500 chars):")
        print(snap[:500])
        
        # Find first ref in snapshot
        import re
        refs = re.findall(r'\[ref=(e\d+)\]', snap)
        if refs:
            ref = refs[0]
            print(f"\nTesting ref resolution for {ref}:")
            
            # Resolve ephemeral ref
            ephemeral = page.locator(f"aria-ref={ref}")
            count = await ephemeral.count()
            print(f"aria-ref={ref} count: {count}")
            
            if count == 1:
                # Normalize to durable locator
                durable = ephemeral.normalize()
                durable_count = await durable.count()
                print(f"normalize() count: {durable_count}")
                print(f"Durable locator works: {durable_count == 1}")
        else:
            print("No refs found in snapshot — check mode='ai' is working")
        
        await b.close()

asyncio.run(test())
```

**Expected output:**
- Snapshot contains `[ref=eN]` strings
- `aria-ref=eN` count is 1
- `normalize()` count is 1

If `normalize()` is not available: your Playwright version is too old. Run `pip install --upgrade playwright` then `playwright install chromium`.

---

## Summary: What Changed in Each Phase File

| File | Change from D Research |
|------|----------------------|
| RULES.md | Added rules 14-16 (no refs in artifacts, normalize(), FastAPI pattern). Added confirmed facts section. |
| PHASE_2_INSTRUCTIONS.md | observe() uses mode="ai", capture_evidence() saves 3 files separately (not just trace), D1 facts added. |
| PHASE_3_INSTRUCTIONS.md | D7 compiler uses normalize() pattern. Bedrock format clarified vs Anthropic SDK. DiscoveryStep added. |
| PHASE_5_INSTRUCTIONS.md | D3 FastAPI pattern (create_task + server.serve()). D6 recorder script (confirmed working). abort() added. |
| GPT_RESEARCH_TASKS.md | All 5 tasks resolved. One optional validation experiment remains. |
