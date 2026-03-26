"""
QA Agent — agentic loop using:
  - OpenAI gpt-4o with tool_use
  - Playwright MCP server (a11y tree as context, structured tool calls for actions)
  - SQLite for run persistence
"""
import asyncio
import json
import os
from pathlib import Path

import openai

import db
from mcp_client import PlaywrightMCPClient, MCPError, extract_screenshot

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
                "Click an element. Identify it from the a11y snapshot using its "
                "role + accessible name, e.g. 'button Login' or 'link Dashboard'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "element": {
                        "type": "string",
                        "description": "Human-readable element description from the a11y tree, e.g. 'button Submit'",
                    },
                    "ref": {
                        "type": "string",
                        "description": "Optional: exact ref ID from the a11y snapshot (e.g. 'e12'). Prefer this over element when available.",
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
            "description": "Type text into an input field. Identify it from the a11y snapshot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "element": {
                        "type": "string",
                        "description": "Human-readable element description, e.g. 'textbox Email'",
                    },
                    "ref": {
                        "type": "string",
                        "description": "Optional: exact ref ID from the a11y snapshot.",
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
                    "element": {"type": "string"},
                    "ref": {"type": "string"},
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
- When clicking or filling: use the `ref` ID from the snapshot when available (e.g. ref="e42")
  It is more precise than the human-readable `element` description
- `assert_element` with `should_exist: false` is correct for testing that errors appear
- Do NOT fabricate results — only report what you actually observed in snapshots
- If a step errors unexpectedly, note it in the summary and continue if possible
- Keep the failure list specific: "assert_element FAIL: 'button Logout' not found after login"
"""


# ---------------------------------------------------------------------------
# Tool executor — translates model tool calls → Playwright MCP calls
# ---------------------------------------------------------------------------

def _save_screenshot(data: bytes, label: str, run_id: int, seq: int) -> str:
    name = f"run{run_id}_step{seq}_{label}.png"
    path = SCREENSHOTS_DIR / name
    path.write_bytes(data)
    return str(path)


def execute_tool(
    tool_name: str,
    tool_input: dict,
    mcp: PlaywrightMCPClient,
    run_id: int,
    seq: int,
) -> tuple[str, str | None]:
    """
    Execute one QA tool call.
    Returns (result_text, screenshot_path_or_None).
    """
    screenshot_path = None

    try:
        # ── Navigation ───────────────────────────────────────────────────────
        if tool_name == "navigate":
            mcp.text_result("browser_navigate", {"url": tool_input["url"]})
            mcp.call_tool("browser_wait_for", {"time": 0.8})
            result = f"Navigated to {tool_input['url']}"

        # ── A11y snapshot ─────────────────────────────────────────────────────
        elif tool_name == "snapshot":
            result = mcp.text_result("browser_snapshot")

        # ── Click ────────────────────────────────────────────────────────────
        elif tool_name == "click":
            args = {"element": tool_input["element"]}
            if ref := tool_input.get("ref"):
                args["ref"] = ref
            mcp.call_tool("browser_click", args)
            mcp.call_tool("browser_wait_for", {"time": 0.4})
            result = f"Clicked: {tool_input['element']}"

        # ── Fill ─────────────────────────────────────────────────────────────
        elif tool_name == "fill":
            ref = tool_input.get("ref")
            if not ref:
                # No ref provided — take snapshot to find it
                snap = mcp.text_result("browser_snapshot")
                label = tool_input["element"].lower()
                for line in snap.splitlines():
                    if label in line.lower() and "ref=" in line:
                        import re
                        m = re.search(r'ref=([^\s\]]+)', line)
                        if m:
                            ref = m.group(1)
                            break
            args = {"text": tool_input["value"]}
            if ref:
                args["ref"] = ref
            else:
                args["element"] = tool_input["element"]
                args["ref"] = ""  # browser_type requires ref; best-effort
            mcp.call_tool("browser_type", args)
            result = f"Filled '{tool_input['element']}' → '{tool_input['value']}'"

        # ── Select ───────────────────────────────────────────────────────────
        elif tool_name == "select_option":
            args = {
                "element": tool_input["element"],
                "values": tool_input["values"],
            }
            if ref := tool_input.get("ref"):
                args["ref"] = ref
            mcp.call_tool("browser_select_option", args)
            result = f"Selected {tool_input['values']} in '{tool_input['element']}'"

        # ── Wait ─────────────────────────────────────────────────────────────
        elif tool_name == "wait_for_load":
            ms = min(int(tool_input.get("ms", 1500)), 5000)
            mcp.call_tool("browser_wait_for", {"time": ms / 1000})
            result = f"Waited {ms}ms"

        # ── Assert: element present/absent ───────────────────────────────────
        elif tool_name == "assert_element":
            snap = mcp.text_result("browser_snapshot")
            desc = tool_input["description"].lower()
            should_exist = tool_input.get("should_exist", True)
            found = desc in snap.lower()

            if should_exist and found:
                result = f"PASS: '{tool_input['description']}' found in a11y tree"
            elif not should_exist and not found:
                result = f"PASS: '{tool_input['description']}' correctly absent"
            else:
                state = "not found" if should_exist else "unexpectedly present"
                blocks = mcp.call_tool("browser_take_screenshot")
                png = extract_screenshot(blocks)
                if png:
                    screenshot_path = _save_screenshot(png, "assert_fail", run_id, seq)
                result = f"FAIL: '{tool_input['description']}' {state} in a11y tree"

        # ── Assert: URL ──────────────────────────────────────────────────────
        elif tool_name == "assert_url":
            blocks = mcp.call_tool("browser_evaluate", {"function": "() => window.location.href"})
            raw = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
            # Extract the actual URL value from the markdown-formatted result
            import re as _re
            m = _re.search(r'"(https?://[^"]+)"', raw)
            current_url = m.group(1) if m else raw.strip()
            needle = tool_input["contains"]
            if needle in current_url:
                result = f"PASS: URL contains '{needle}' (current: {current_url})"
            else:
                blocks_ss = mcp.call_tool("browser_take_screenshot")
                png = extract_screenshot(blocks_ss)
                if png:
                    screenshot_path = _save_screenshot(png, "url_fail", run_id, seq)
                result = f"FAIL: URL does not contain '{needle}'. Got: {current_url}"

        # ── Assert: text present ─────────────────────────────────────────────
        elif tool_name == "assert_text_present":
            snap = mcp.text_result("browser_snapshot")
            text = tool_input["text"]
            if text.lower() in snap.lower():
                result = f"PASS: text '{text}' found on page"
            else:
                blocks = mcp.call_tool("browser_take_screenshot")
                png = extract_screenshot(blocks)
                if png:
                    screenshot_path = _save_screenshot(png, "text_fail", run_id, seq)
                result = f"FAIL: text '{text}' not found on page"

        # ── Screenshot ───────────────────────────────────────────────────────
        elif tool_name == "screenshot":
            label = tool_input.get("label", "manual")
            blocks = mcp.call_tool("browser_take_screenshot")
            png = extract_screenshot(blocks)
            if png:
                screenshot_path = _save_screenshot(png, label, run_id, seq)
                result = f"Screenshot saved: {screenshot_path}"
            else:
                result = "Screenshot: no image data returned"

        # ── Test done ────────────────────────────────────────────────────────
        elif tool_name == "test_done":
            result = "TEST_DONE"

        else:
            result = f"Unknown tool: {tool_name}"

    except MCPError as e:
        blocks = mcp.call_tool("browser_take_screenshot") if tool_name != "screenshot" else []
        png = extract_screenshot(blocks)
        if png:
            screenshot_path = _save_screenshot(png, f"mcp_error_{tool_name}", run_id, seq)
        result = f"ERROR (MCP {e.code}): {e}"

    except Exception as e:
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

        with PlaywrightMCPClient() as mcp:
            mcp.initialize()

            messages = [
                {
                    "role": "user",
                    "content": (
                        f"Base URL: {base_url}\n\n"
                        f"Test scenario:\n{scenario}"
                    ),
                }
            ]

            seq = 0
            done = False
            max_steps = 40

            while seq < max_steps and not done:
                api_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

                response = client.chat.completions.create(
                    model="gpt-4o",
                    max_tokens=4096,
                    messages=api_messages,
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
                        tool_name, tool_input, mcp, run_id, seq
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
