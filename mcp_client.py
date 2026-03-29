"""
MCP client using the standard streamable-HTTP transport.

  POST /mcp  — send JSON-RPC request, get response directly in HTTP body
  Uses Mcp-Session-Id header to maintain session across requests.

No persistent SSE connection, no background threads.
"""
import json
import os
import threading
import base64
import httpx

_MCP_URL = os.getenv("PLAYWRIGHT_MCP_URL", "http://localhost:8931/mcp")
# Normalise: always end with /mcp
if not _MCP_URL.rstrip("/").endswith("/mcp"):
    _MCP_URL = _MCP_URL.rstrip("/") + "/mcp"


class MCPError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


class PlaywrightMCPClient:
    """
    Synchronous MCP client over streamable-HTTP transport.

    Each call is a plain HTTP POST to /mcp — no persistent SSE connection.
    Session is maintained via the Mcp-Session-Id response header.
    """

    def __init__(self, mcp_url: str = _MCP_URL, timeout: float = 90.0):
        self._url = mcp_url
        self._timeout = timeout
        self._http = httpx.Client(timeout=timeout)
        self._session_id: str | None = None
        self._req_id = 0
        self._lock = threading.Lock()
        self._initialized = False

    # ── Internal ──────────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        with self._lock:
            self._req_id += 1
            return self._req_id

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _rpc(self, method: str, params: dict | None = None, _retry: bool = True) -> dict:
        payload: dict = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params:
            payload["params"] = params

        resp = self._http.post(self._url, json=payload, headers=self._headers())

        # Debug logging — set MCP_DEBUG=1 to enable
        if os.getenv("MCP_DEBUG"):
            import sys
            print(
                f"[mcp_debug] {method} → HTTP {resp.status_code} "
                f"ct={resp.headers.get('content-type','?')} "
                f"body={resp.text[:500]!r}",
                file=sys.stderr,
            )

        # Capture / refresh session ID from every response
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid

        # Session expired or server restarted — reinitialize once and retry
        if resp.status_code == 404 and _retry and method != "initialize":
            self._session_id = None
            self._initialized = False
            self.initialize()
            return self._rpc(method, params, _retry=False)

        if resp.status_code not in (200, 202):
            raise MCPError(-1, f"HTTP {resp.status_code} from MCP server: {resp.text[:200]}")

        # 202 Accepted with empty body is valid for notifications / long-running ops
        if resp.status_code == 202 or not resp.text.strip():
            return {}

        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            result_msg = _parse_sse_body(resp.text)
        else:
            result_msg = resp.json()

        if "error" in result_msg:
            err = result_msg["error"]
            raise MCPError(err.get("code", -1), err.get("message", "MCP error"), err.get("data"))

        return result_msg.get("result", {})

    def _notify(self, method: str, params: dict | None = None) -> None:
        """One-way notification — no id, no response expected."""
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        try:
            self._http.post(self._url, json=payload, headers=self._headers())
        except Exception:
            pass  # notifications are fire-and-forget

    # ── Public API ────────────────────────────────────────────────────────────

    def initialize(self) -> dict:
        if self._initialized:
            return {}
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

    def close(self) -> None:
        if self._session_id:
            try:
                self._http.delete(self._url, headers=self._headers())
            except Exception:
                pass
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_sse_body(text: str) -> dict:
    """Extract the first data: line from an SSE-encoded response body."""
    for line in text.splitlines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if data:
                try:
                    return json.loads(data)
                except json.JSONDecodeError as exc:
                    raise MCPError(-1, f"Invalid JSON in SSE data line: {data[:200]}") from exc
    # No data: line found — treat as empty successful response (e.g. notification ACK)
    import sys
    print(f"[mcp_client] WARNING: SSE response had no data: line. Body was: {text[:300]!r}", file=sys.stderr)
    return {}


def extract_screenshot(content_blocks: list[dict]) -> bytes | None:
    for block in content_blocks:
        if block.get("type") == "image":
            return base64.b64decode(block["data"])
    return None
