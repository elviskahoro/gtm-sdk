"""JavaScript snippets for interacting with Gmail's DOM via Playwright."""

import re
from urllib.parse import unquote

JS_GET_EMAIL_ROW_COUNT = """() => {
    const main = document.querySelector('[role="main"]');
    if (!main) return 0;
    return main.querySelectorAll('tr.zA').length;
}"""

JS_LIST_EMAIL_THREAD_KEYS = """() => {
    const main = document.querySelector('[role="main"]');
    if (!main) return [];

    const rows = Array.from(main.querySelectorAll('tr.zA'));
    const keys = [];
    const seen = new Set();

    for (const row of rows) {
        const node =
            row.querySelector('[data-thread-id][data-legacy-thread-id]') ||
            row.querySelector('[data-thread-id]');
        if (!node) continue;

        const threadKey = node.getAttribute('data-thread-id');
        const legacyKey = node.getAttribute('data-legacy-thread-id');
        const dedupe = `${threadKey || ""}|${legacyKey || ""}`;
        if (seen.has(dedupe)) continue;
        seen.add(dedupe);

        keys.push({
            thread_key: threadKey,
            legacy_key: legacyKey,
        });
    }

    return keys;
}"""

JS_CLICK_NEXT_RESULTS = """() => {
    const candidates = Array.from(
      document.querySelectorAll('[aria-label^="Next results"], [data-tooltip^="Next results"]')
    );
    const visible = candidates.filter(el => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    });

    const next = visible.find(el => {
      const disabled = el.getAttribute('aria-disabled') === 'true';
      const className = (el.className || '').toString();
      const visuallyDisabled = className.includes('T-I-JE');
      return !disabled && !visuallyDisabled;
    });

    if (!next) return false;
    next.click();
    return true;
}"""

JS_EXTRACT_METADATA = """() => {
    const subjectEl = document.querySelector('h2[data-thread-perm-id], h2.hP');
    const senderEl = document.querySelector('[email], .gD');
    const dateEl = document.querySelector('.g3, [data-tooltip]');
    return {
        subject: subjectEl ? subjectEl.textContent.trim() : 'unknown',
        sender: senderEl
            ? (senderEl.getAttribute('email') || senderEl.textContent.trim())
            : 'unknown',
        date: dateEl ? dateEl.textContent.trim() : 'unknown',
    };
}"""

JS_SCROLL_TO_BOTTOM = """() => {
    const main = document.querySelector('[role="main"]');
    if (main) main.scrollTo(0, main.scrollHeight);
}"""

JS_SCROLL_TO_TOP = """() => {
    const main = document.querySelector('[role="main"]');
    if (main) main.scrollTo(0, 0);
}"""

JS_HIDE_CHROME = """() => {
    // Hide all siblings/ancestors' siblings to isolate main content
    document.querySelectorAll('body > *').forEach(el => {
        if (!el.contains(document.querySelector('[role="main"]'))) {
            el.style.display = 'none';
        }
    });
    // Hide sidebar, header, toolbar, and tabs
    const selectors = [
        '[role="navigation"]',
        '[role="banner"]',
        'header',
        '.aeN',
        '.aeG',
        '.nH.oy8Mbf',
        '.aeH',
        '.G-atb',
    ];
    selectors.forEach(s => {
        document.querySelectorAll(s).forEach(el => el.style.display = 'none');
    });
    // Make main content full width
    const main = document.querySelector('[role="main"]');
    if (main) {
        main.style.position = 'fixed';
        main.style.left = '0';
        main.style.top = '0';
        main.style.width = '100vw';
        main.style.height = 'auto';
        main.style.zIndex = '99999';
        main.style.background = 'white';
    }
}"""


def parse_thread_id(url: str) -> str | None:
    """Extract Gmail thread ID from a URL like #inbox/FMfcg..."""
    if "/mail/" not in url or "#" not in url:
        return None
    fragment = unquote(url.split("#", 1)[1])
    fragment = fragment.split("?", 1)[0]
    parts = fragment.split("/")
    thread_id = parts[-1] if len(parts) >= 2 else None
    if thread_id and re.fullmatch(r"[A-Za-z0-9_-]{8,}", thread_id):
        return thread_id
    if thread_id and re.fullmatch(r"thread-[a-z]:[0-9]{8,}", thread_id):
        return thread_id.replace(":", "_")
    return None
