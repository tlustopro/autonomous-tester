"""
MCP client using Playwright MCP's legacy SSE transport.

  GET  /sse            — open SSE stream; server sends "endpoint" event with POST URL
  POST /sse?sessionId= — send JSON-RPC requests; responses arrive via GET stream
"""
import json
import os
import threading
import httpx

_url = os.getenv("PLAYWRIGHT_MCP_URL", "http://localhost:8931/mcp")
# Strip /mcp or /sse suffix to get base URL
_BASE_URL = _url.rstrip("/").removesuffix("/mcp").removesuffix("/sse")


class MCPError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


class PlaywrightMCPClient:
    """
    Synchronous MCP client over Playwright MCP legacy SSE transport.

    - Background thread maintains GET /sse connection.
    - Tool calls POST JSON-RPC to the session URL.
    - Responses are matched by request ID from the SSE stream.
    """

    def __init__(self, base_url: str = _BASE_URL, timeout: float = 90.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._http = httpx.Client(timeout=timeout)
        self._post_url: str | None = None
        self._pending: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._req_id = 0
        self._ready = threading.Event()
        self._initialized = False

    # ── SSE stream ────────────────────────────────────────────────────────────

    def _sse_reader(self):
        """Background thread: reads the SSE stream and wakes waiting _rpc calls."""
        try:
            with httpx.stream(
                "GET",
                f"{self._base_url}/sse",
                headers={"Accept": "text/event-stream"},
                timeout=None,
            ) as resp:
                event_type = ""
                for line in resp.iter_lines():
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()
                        if not data:
                            continue
                        if event_type == "endpoint":
                            path = (
                                data
                                if data.startswith("http")
                                else f"{self._base_url}{data}"
                            )
                            self._post_url = path
                            self._ready.set()
                        else:
                            try:
                                msg = json.loads(data)
                                req_id = msg.get("id")
                                if req_id is not None:
                                    with self._lock:
                                        entry = self._pending.get(req_id)
                                        if entry:
                                            entry["result"] = msg
                                            entry["event"].set()
                            except json.JSONDecodeError:
                                pass
        except Exception:
            pass

    def _start(self):
        t = threading.Thread(target=self._sse_reader, daemon=True)
        t.start()
        if not self._ready.wait(timeout=60):
            raise MCPError(-1, "Timeout connecting to MCP SSE stream at /sse")

    # ── JSON-RPC ──────────────────────────────────────────────────────────────

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            self._req_id += 1
            req_id = self._req_id
            event = threading.Event()
            self._pending[req_id] = {"event": event, "result": None}

        payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            payload["params"] = params

        resp = self._http.post(
            self._post_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code not in (200, 202):
            with self._lock:
                self._pending.pop(req_id, None)
            raise MCPError(-1, f"HTTP {resp.status_code} from MCP server")

        if not event.wait(timeout=self._timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise MCPError(-1, f"Timeout waiting for response to '{method}'")

        with self._lock:
            result_msg = self._pending.pop(req_id)["result"]

        if "error" in result_msg:
            err = result_msg["error"]
            raise MCPError(err.get("code", -1), err.get("message", "MCP error"), err.get("data"))

        return result_msg.get("result", {})

    def _notify(self, method: str, params: dict | None = None):
        """Send a one-way MCP notification (no response expected)."""
        payload = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        self._http.post(
            self._post_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def initialize(self) -> dict:
        if self._initialized:
            return {}
        self._start()
        result = self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "qa-agent", "version": "1.0"},
        })
        self._notify("notifications/initialized")
        self._initialized = True
        return result

    def list_tools(self) -> list[dict]:
        self.initialize()
        result = self._rpc("tools/list")
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> list[dict]:
        """Call a Playwright MCP tool; returns list of content blocks."""
        self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        return result.get("content", [])

    def text_result(self, name: str, arguments: dict | None = None) -> str:
        """Call tool and return joined text content."""
        blocks = self.call_tool(name, arguments)
        return "\n".join(b["text"] for b in blocks if b.get("type") == "text")

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ---------------------------------------------------------------------------
# Helper — extract screenshot bytes from a tool result if present
# ---------------------------------------------------------------------------
def extract_screenshot(content_blocks: list[dict]) -> bytes | None:
    import base64
    for block in content_blocks:
        if block.get("type") == "image":
            return base64.b64decode(block["data"])
    return None
