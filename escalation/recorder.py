"""
escalation/recorder.py
Captures human actions during escalation via Playwright's add_init_script + expose_binding.

D6 research confirmed:
- add_init_script persists across ALL page navigations automatically
- expose_binding exposes function in every frame of every page in the context
- NEVER capture element.value — only capture field identity

Two-layer PII protection:
1. Client-side: JS never reads element.value
2. Server-side: redact_log_entry() applied before writing to disk
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext

from escalation.controller import EscalationController


# D6 confirmed: this script is injected into every page/frame on navigation automatically
RECORDER_SCRIPT = r"""
(() => {
    // Guard against double-injection
    if (window.__humanRecorderInstalled) return;
    window.__humanRecorderInstalled = true;

    function getLabel(element) {
        // Priority: aria-label > aria-labelledby > associated <label> > name attr > placeholder
        const ariaLabel = element.getAttribute('aria-label');
        if (ariaLabel) return ariaLabel;

        const labelledBy = element.getAttribute('aria-labelledby');
        if (labelledBy) {
            const labelEl = document.getElementById(labelledBy);
            if (labelEl) return labelEl.textContent.trim();
        }

        if ('labels' in element && element.labels && element.labels.length > 0) {
            return element.labels[0].textContent.trim();
        }

        return element.getAttribute('name') || element.getAttribute('placeholder') || 'unknown';
    }

    function getDescriptor(element) {
        if (!(element instanceof Element)) return null;
        return {
            tag: element.tagName.toLowerCase(),
            role: element.getAttribute('role') || null,
            label: getLabel(element),
            input_type: element instanceof HTMLInputElement ? element.type : null,
            // INTENTIONALLY NOT CAPTURED: element.value
            // Never capture what the human typed — only WHERE they typed
        };
    }

    function send(type, target, extra) {
        if (typeof window.__pwRecordEvent !== 'function') return;
        const payload = {
            type,
            timestamp_ms: Date.now(),
            target: getDescriptor(target),
            ...extra,
        };
        void window.__pwRecordEvent(payload).catch(() => {});
    }

    document.addEventListener('click', (e) => {
        let el = e.target;
        if (el instanceof Element) {
            el = el.closest('button,a,input,select,textarea,[role]') || el;
        }
        send('click', el, {});
    }, true);

    document.addEventListener('input', (e) => {
        // Field identity only — NO value
        send('input', e.target, {});
    }, true);

    document.addEventListener('change', (e) => {
        send('change', e.target, {});
    }, true);

    document.addEventListener('submit', (e) => {
        const form = e.target;
        send('submit', form, {
            form_action: form instanceof HTMLFormElement ? form.action : null,
            form_method: form instanceof HTMLFormElement ? form.method : null,
        });
    }, true);
})();
"""


async def attach_recorder(
    context: BrowserContext,
    controller: EscalationController,
    output_path: Path,
) -> None:
    """
    Attach human action recorder to browser context.

    D6 confirmed: add_init_script persists automatically — no reinjection needed.
    expose_binding is available in every frame of every page in the context.

    Call this ONCE when creating the BrowserContext.
    It continues recording for the entire session (including during automation).
    During non-escalation periods, recordings are benign (just navigation/clicks).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_lock = asyncio.Lock()

    def append_line(line: str) -> None:
        with output_path.open("a", encoding="utf-8") as f:
            f.write(line)

    async def record_event(source, payload) -> None:
        from policy.redactor import redact_log_entry

        record = {
            "page_url": source["page"].url,
            "frame_url": source["frame"].url,
            "ts": datetime.utcnow().isoformat() + "Z",
            **payload,
        }

        # Server-side redaction as second layer of defense
        record = redact_log_entry(record)
        controller.add_human_action(record)

        line = json.dumps(record, ensure_ascii=False) + "\n"
        async with write_lock:
            await asyncio.to_thread(append_line, line)

    # Install binding FIRST, then init script
    await context.expose_binding("__pwRecordEvent", record_event)
    await context.add_init_script(script=RECORDER_SCRIPT)
