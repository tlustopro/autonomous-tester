"""
QA Agent — agentic loop using:
  - OpenAI gpt-4o with tool_use
  - Playwright (Python) for browser automation
  - SQLite for run persistence
"""
import asyncio
import base64
import json
import os
import re
from pathlib import Path

import openai
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

import db

SCREENSHOTS_DIR = Path(os.getenv("SCREENSHOTS_DIR", "screenshots"))
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Tools we expose to the model (OpenAI function-calling format)
# ---------------------------------------------------------------------------
QA_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate browser to a URL and wait for the page to load.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Absolute URL"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot",
            "description": (
                "Get the current page's accessibility tree (a11y snapshot). "
                "Returns structured text describing every interactive and visible element: "
                "role, name, state, relationships. Use this to orient yourself and pick "
                "precise element references for click/fill."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": (
                "Click an element. Use the exact description from the snapshot. "
                "For ARIA elements use 'role name' (e.g. 'button Close', 'link Dashboard'). "
                "For elements with a test ID use '[data-test-id=CloseIcon]' or '[data-testid=submit-btn]'. "
                "For elements with aria-label use '[aria-label=Close dialog]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element": {
                        "type": "string",
                        "description": (
                            "Element identifier. Examples: 'button Submit', 'link Dashboard', "
                            "'[data-test-id=CloseIcon]', '[data-testid=search-btn]', '[aria-label=Close]'"
                        ),
                    },
                },
                "required": ["element"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fill",
            "description": (
                "Type text into an input field. "
                "Use 'textbox Label' for labeled inputs, "
                "'[placeholder=Search...]' for placeholder-identified inputs, "
                "or '[data-testid=search-input]' for test-ID-identified inputs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element": {
                        "type": "string",
                        "description": (
                            "Input identifier. Examples: 'textbox Email', "
                            "'[placeholder=Search...]', '[data-testid=email-input]'"
                        ),
                    },
                    "value": {"type": "string", "description": "Text to type"},
                },
                "required": ["element", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_option",
            "description": "Select an option in a <select> dropdown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "element": {"type": "string", "description": "Element description, e.g. 'combobox Country'"},
                    "values": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Option values or labels to select",
                    },
                },
                "required": ["element", "values"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for_load",
            "description": "Wait briefly for navigation or dynamic content to settle.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ms": {
                        "type": "integer",
                        "description": "Milliseconds to wait (default 1500, max 5000)",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assert_element",
            "description": (
                "Assert that an element exists and is visible in the current a11y tree. "
                "Returns PASS or FAIL with details. Takes a screenshot on failure."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "What you expect to find, e.g. 'button Logout' or 'heading Dashboard'",
                    },
                    "should_exist": {
                        "type": "boolean",
                        "description": "True = must be present, False = must be absent. Default true.",
                    },
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assert_url",
            "description": "Assert the current URL contains a given substring.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contains": {"type": "string", "description": "Substring the URL must contain"},
                },
                "required": ["contains"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assert_text_present",
            "description": "Assert that a specific text string is visible anywhere on the page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text that must be present on page"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Take a screenshot of the current page and save it. Use for debugging or to capture evidence of a failure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Filename label, e.g. 'after_login'"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_done",
            "description": "Signal that the test scenario is fully complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "passed": {"type": "boolean"},
                    "summary": {
                        "type": "string",
                        "description": "1-3 sentence summary of what was tested and the outcome.",
                    },
                    "failures": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of specific assertion failures. Empty if passed.",
                    },
                },
                "required": ["passed", "summary"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are a precise, autonomous QA testing agent.

You control a real browser through structured tools backed by Playwright's accessibility tree.
The a11y tree gives you role + name + state for every element — NO CSS selectors needed.

## Workflow for every scenario

1. `navigate` to the base URL first
2. `snapshot` to understand the current page
3. Execute the scenario step by step
4. Use `assert_element`, `assert_url`, `assert_text_present` to verify expected states
5. Take a `screenshot` immediately before calling `test_done` if any step failed
6. Call `test_done` with a clear, honest summary

## Rules

- Always `snapshot` after navigation or a major interaction — never assume page state
- When clicking or filling: use the exact role + name from the ARIA tree, e.g. 'button Sign in' or 'textbox Email'
- When the snapshot shows a "DOM attributes" section: prefer '[data-test-id=X]', '[data-testid=X]',
  '[aria-label=X]', or '[placeholder=X]' syntax — these are the most stable identifiers
- data-test-id/data-testid beats text matching — always use it when available
- `assert_element` with `should_exist: false` is correct for testing that errors appear
- Do NOT fabricate results — only report what you actually observed in snapshots
- If a step errors unexpectedly, note it in the summary and continue if possible
- Keep the failure list specific: "assert_element FAIL: 'button Logout' not found after login"
"""


# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------

def _snapshot(page: Page) -> str:
    """
    Return page state for the LLM:
    1. ARIA tree (roles, names, states)
    2. Supplemental DOM attributes section for elements invisible to the ARIA tree
       (data-testid, data-test-id, aria-label, placeholder, title)
    """
    aria = page.locator("body").aria_snapshot()

    dom_attrs = page.evaluate("""() => {
        const sel = '[data-testid],[data-test-id],[placeholder],[title],[aria-label]';
        return Array.from(document.querySelectorAll(sel)).map(el => {
            const attrs = {};
            if (el.getAttribute('data-testid'))  attrs['data-testid']  = el.getAttribute('data-testid');
            if (el.getAttribute('data-test-id')) attrs['data-test-id'] = el.getAttribute('data-test-id');
            if (el.getAttribute('aria-label'))   attrs['aria-label']   = el.getAttribute('aria-label');
            if (el.getAttribute('placeholder'))  attrs['placeholder']  = el.getAttribute('placeholder');
            if (el.getAttribute('title') && el.getAttribute('title') !== el.textContent.trim())
                                                 attrs['title']        = el.getAttribute('title');
            return {
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role') || '',
                text: (el.textContent || '').trim().slice(0, 60),
                attrs
            };
        }).filter(e => Object.keys(e.attrs).length > 0);
    }""")

    lines = [aria or "(empty accessibility tree)"]
    if dom_attrs:
        lines.append("\n--- DOM attributes (use these to identify elements) ---")
        for el in dom_attrs:
            parts = [f"<{el['tag']}"]
            if el['role']:
                parts.append(f" role={el['role']}")
            for k, v in el['attrs'].items():
                parts.append(f' {k}="{v}"')
            if el['text']:
                parts.append(f' text="{el["text"]}"')
            parts.append(">")
            lines.append("".join(parts))

    return "\n".join(lines)


def _first_visible(loc):
    """Return the first visible match from a locator; fallback to .first."""
    try:
        count = loc.count()
        for i in range(min(count, 5)):
            el = loc.nth(i)
            if el.is_visible():
                return el
    except Exception:
        pass
    return loc.first


def _try(fn):
    """Call fn(), return first visible match if count > 0, else None."""
    try:
        loc = fn()
        if loc is not None and loc.count() > 0:
            return _first_visible(loc)
    except Exception:
        pass
    return None


def _with_retry(fn, retries: int = 1, wait_ms: int = 1000):
    """Run fn(); on PlaywrightTimeoutError retry up to `retries` times."""
    import time
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except PWTimeout as e:
            last_exc = e
            if attempt < retries:
                time.sleep(wait_ms / 1000)
    raise last_exc


def _get_locator(page: Page, element: str):
    """
    Resolve element description to a Playwright locator.

    Supported formats:
    - 'button Submit'            → get_by_role("button", name="Submit")
    - 'textbox Email'            → get_by_role / get_by_label / get_by_placeholder
    - '[data-test-id=CloseIcon]' → page.locator("[data-test-id='CloseIcon']")
    - '[data-testid=CloseIcon]'  → get_by_test_id("CloseIcon")
    - '[aria-label=Close]'       → page.locator("[aria-label='Close']")
    - '[placeholder=Search]'     → get_by_placeholder("Search")
    - '[title=Close]'            → get_by_title("Close")
    """
    elem = element.strip()

    # Explicit [attr=value] syntax — highest priority
    m = re.match(r'^\[([^\]=]+)=["\']?([^"\'\]]+)["\']?\]$', elem)
    if m:
        attr, value = m.group(1).lower(), m.group(2)
        if attr == 'data-testid':
            loc = _try(lambda: page.get_by_test_id(value))
            if loc:
                return loc
        for quote in ("'", '"'):
            loc = _try(lambda q=quote: page.locator(f"[{attr}={q}{value}{q}]"))
            if loc:
                return loc

    # Parse "role name" format
    parts = elem.split(None, 1)
    name_str = parts[1].strip('"') if len(parts) == 2 else elem

    candidates = []
    if len(parts) == 2:
        role_str = parts[0].lower()
        candidates = [
            lambda: page.get_by_role(role_str, name=name_str, exact=False),
            lambda: page.get_by_label(name_str, exact=False),
            lambda: page.get_by_placeholder(name_str, exact=False),
            lambda: page.get_by_title(name_str, exact=False),
            lambda: page.get_by_test_id(name_str),
            lambda: page.locator(f"[data-test-id='{name_str}']"),
            lambda: page.locator(f"[aria-label='{name_str}']"),
        ]
    else:
        # Single word — treat as role name (e.g. just "textbox", "button")
        role_str = elem.lower()
        candidates = [lambda: page.get_by_role(role_str)]

    for fn in candidates:
        loc = _try(fn)
        if loc:
            return loc

    return None


def _save_screenshot(data: bytes, label: str, run_id: int, seq: int) -> str:
    name = f"run{run_id}_step{seq}_{label}.png"
    path = SCREENSHOTS_DIR / name
    path.write_bytes(data)
    return str(path)


# ---------------------------------------------------------------------------
# Tool executor — translates model tool calls → Playwright calls
# ---------------------------------------------------------------------------

def execute_tool(
    tool_name: str,
    tool_input: dict,
    page: Page,
    run_id: int,
    seq: int,
) -> tuple[str, str | None]:
    """
    Execute one QA tool call against a live Playwright page.
    Returns (result_text, screenshot_path_or_None).
    """
    screenshot_path = None

    try:
        # ── Navigation ───────────────────────────────────────────────────────
        if tool_name == "navigate":
            page.goto(tool_input["url"], wait_until="domcontentloaded", timeout=30_000)
            # Silently wait for networkidle — SPAs with WebSocket/polling never reach it
            try:
                page.wait_for_load_state("networkidle", timeout=5_000)
            except PWTimeout:
                pass
            result = f"Navigated to {tool_input['url']}"

        # ── A11y snapshot ─────────────────────────────────────────────────────
        elif tool_name == "snapshot":
            result = _snapshot(page)

        # ── Click ────────────────────────────────────────────────────────────
        elif tool_name == "click":
            loc = _get_locator(page, tool_input["element"])
            if loc is None:
                png = page.screenshot()
                screenshot_path = _save_screenshot(png, "not_found", run_id, seq)
                result = f"FAIL: element not found: '{tool_input['element']}'"
            else:
                _with_retry(lambda: loc.click(timeout=15_000), retries=1, wait_ms=1000)
                # Silently wait for networkidle — don't block on SPAs
                try:
                    page.wait_for_load_state("networkidle", timeout=3_000)
                except PWTimeout:
                    pass
                result = f"Clicked: {tool_input['element']}"

        # ── Fill ─────────────────────────────────────────────────────────────
        elif tool_name == "fill":
            loc = _get_locator(page, tool_input["element"])
            if loc is None:
                png = page.screenshot()
                screenshot_path = _save_screenshot(png, "not_found", run_id, seq)
                result = f"FAIL: element not found: '{tool_input['element']}'"
            else:
                # click() first to focus and trigger React onFocus; fill() clears + types + fires events
                loc.click(timeout=10_000)
                _with_retry(lambda: loc.fill(tool_input["value"], timeout=10_000), retries=1, wait_ms=500)
                result = f"Filled '{tool_input['element']}' → '{tool_input['value']}'"

        # ── Select ───────────────────────────────────────────────────────────
        elif tool_name == "select_option":
            loc = _get_locator(page, tool_input["element"])
            if loc is None:
                png = page.screenshot()
                screenshot_path = _save_screenshot(png, "not_found", run_id, seq)
                result = f"FAIL: element not found: '{tool_input['element']}'"
            else:
                loc.select_option(tool_input["values"], timeout=10_000)
                result = f"Selected {tool_input['values']} in '{tool_input['element']}'"

        # ── Wait ─────────────────────────────────────────────────────────────
        elif tool_name == "wait_for_load":
            ms = min(int(tool_input.get("ms", 1500)), 5000)
            page.wait_for_timeout(ms)
            result = f"Waited {ms}ms"

        # ── Assert: element present/absent ───────────────────────────────────
        elif tool_name == "assert_element":
            desc = tool_input["description"]
            should_exist = tool_input.get("should_exist", True)
            try:
                loc = _get_locator(page, desc)
                found = loc is not None and loc.is_visible(timeout=3_000)
            except Exception:
                found = False

            if should_exist and found:
                result = f"PASS: '{desc}' is visible"
            elif not should_exist and not found:
                result = f"PASS: '{desc}' correctly absent"
            else:
                state = "not visible" if should_exist else "unexpectedly visible"
                png = page.screenshot()
                screenshot_path = _save_screenshot(png, "assert_fail", run_id, seq)
                result = f"FAIL: '{desc}' {state}"

        # ── Assert: URL ──────────────────────────────────────────────────────
        elif tool_name == "assert_url":
            current_url = page.url
            needle = tool_input["contains"]
            if needle in current_url:
                result = f"PASS: URL contains '{needle}' (current: {current_url})"
            else:
                png = page.screenshot()
                screenshot_path = _save_screenshot(png, "url_fail", run_id, seq)
                result = f"FAIL: URL does not contain '{needle}'. Got: {current_url}"

        # ── Assert: text present ─────────────────────────────────────────────
        elif tool_name == "assert_text_present":
            text = tool_input["text"]
            try:
                loc = page.get_by_text(text, exact=False)
                found = loc.first.is_visible(timeout=3_000)
            except Exception:
                found = False
            if found:
                result = f"PASS: text '{text}' found on page"
            else:
                png = page.screenshot()
                screenshot_path = _save_screenshot(png, "text_fail", run_id, seq)
                result = f"FAIL: text '{text}' not found on page"

        # ── Screenshot ───────────────────────────────────────────────────────
        elif tool_name == "screenshot":
            label = tool_input.get("label", "manual")
            png = page.screenshot()
            screenshot_path = _save_screenshot(png, label, run_id, seq)
            result = f"Screenshot saved: {screenshot_path}"

        # ── Test done ────────────────────────────────────────────────────────
        elif tool_name == "test_done":
            result = "TEST_DONE"

        else:
            result = f"Unknown tool: {tool_name}"

    except Exception as e:
        try:
            png = page.screenshot()
            screenshot_path = _save_screenshot(png, f"error_{tool_name}", run_id, seq)
        except Exception:
            pass
        result = f"ERROR: {e}"

    return result, screenshot_path


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

async def run_test(
    scenario: str,
    base_url: str,
    on_step=None,
) -> dict:
    """
    Execute a test scenario.

    Args:
        scenario:  Natural language test instructions
        base_url:  Base URL of the application under test
        on_step:   Async callback(step_dict) — called after each tool execution

    Returns dict: {run_id, passed, summary, failures, steps, screenshots}
    """
    run_id = db.create_run(scenario, base_url)

    loop = asyncio.get_event_loop()

    def _notify_step(step: dict):
        """Bridge: fire the async on_step callback from the sync thread."""
        if on_step:
            future = asyncio.run_coroutine_threadsafe(on_step(step), loop)
            future.result()

    def _run_sync() -> dict:
        results = {
            "run_id": run_id,
            "passed": False,
            "summary": "",
            "failures": [],
            "steps": [],
            "screenshots": [],
        }

        client = openai.OpenAI()

        browser_name = os.getenv("BROWSER", "chromium").lower()

        with sync_playwright() as pw:
            launcher = {"chromium": pw.chromium, "firefox": pw.firefox, "webkit": pw.webkit}.get(
                browser_name, pw.chromium
            )
            browser = launcher.launch(headless=True)
            page = browser.new_page()

            try:
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Base URL: {base_url}\n\n"
                            f"Test scenario:\n{scenario}"
                        ),
                    },
                ]

                seq = 0
                done = False
                max_steps = 40

                while seq < max_steps and not done:
                    response = client.chat.completions.create(
                        model="gpt-4o",
                        max_tokens=4096,
                        messages=messages,
                        tools=QA_TOOLS,
                    )

                    choice = response.choices[0]
                    msg = choice.message

                    assistant_entry = {"role": "assistant", "content": msg.content}
                    if msg.tool_calls:
                        assistant_entry["tool_calls"] = [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                            }
                            for tc in msg.tool_calls
                        ]
                    messages.append(assistant_entry)

                    tool_calls = msg.tool_calls or []

                    for tc in tool_calls:
                        tool_name = tc.function.name
                        tool_input = json.loads(tc.function.arguments)

                        seq += 1
                        result_text, screenshot_path = execute_tool(
                            tool_name, tool_input, page, run_id, seq
                        )

                        db.add_step(
                            run_id=run_id,
                            seq=seq,
                            tool=tool_name,
                            input_data=tool_input,
                            result=result_text,
                            screenshot_path=screenshot_path,
                        )

                        step = {
                            "step": seq,
                            "tool": tool_name,
                            "input": tool_input,
                            "result": result_text,
                            "screenshot": screenshot_path,
                        }
                        results["steps"].append(step)
                        if screenshot_path:
                            results["screenshots"].append(screenshot_path)

                        _notify_step(step)

                        if tool_name == "test_done":
                            results["passed"] = tool_input.get("passed", False)
                            results["summary"] = tool_input.get("summary", "")
                            results["failures"] = tool_input.get("failures", [])
                            done = True
                            result_text = "Acknowledged."

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result_text,
                        })

                    if not tool_calls and choice.finish_reason == "stop" and not done:
                        results["summary"] = "Agent finished without calling test_done."
                        break

            finally:
                browser.close()

        if done:
            db.finish_run(
                run_id,
                results["passed"],
                results["summary"],
                results["failures"],
            )
        else:
            db.fail_run(run_id, results["summary"] or "Max steps reached")

        return results

    return await asyncio.to_thread(_run_sync)
