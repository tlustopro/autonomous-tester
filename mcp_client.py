"""
MCP client using the standard streamable-HTTP transport.

  POST /mcp  — send JSON-RPC request, get response via SSE stream
  Uses Mcp-Session-Id header to maintain session across requests.

Uses httpx streaming to properly wait for SSE data lines.
"""
import json
import os
import threading
import base64
import sys
import httpx

_MCP_URL = os.getenv("PLAYWRIGHT_MCP_URL", "http://localhost:8931/mcp")
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

    Uses httpx streaming to correctly read SSE responses where the server
    may send headers immediately but data lines after a delay.
    """

    def __init__(self, mcp_url: str = _MCP_URL, timeout: float = 90.0):
        self._url = mcp_url
        self._timeout = timeout
        self._http = httpx.Client(timeout=timeout)
        self._session_id: str | None = None
        self._req_id = 0
        self._lock = threading.Lock()
        self._initialized = False

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

        with self._http.stream("POST", self._url, json=payload, headers=self._headers()) as resp:
            # Capture session ID from response headers (before reading body)
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid

            # Session expired — reinitialize once and retry
            if resp.status_code == 404 and _retry and method != "initialize":
                resp.read()
                self._session_id = None
                self._initialized = False
                self.initialize()
                return self._rpc(method, params, _retry=False)

            if resp.status_code not in (200, 202):
                body = resp.read().decode(errors="replace")
                raise MCPError(-1, f"HTTP {resp.status_code} from MCP server: {body[:200]}")

            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                result_msg = _read_sse_stream(resp)
            else:
                body = resp.read().decode(errors="replace")
                result_msg = json.loads(body) if body.strip() else {}

        if not result_msg:
            return {}

        if "error" in result_msg:
            err = result_msg["error"]
            raise MCPError(err.get("code", -1), err.get("message", "MCP error"), err.get("data"))

        result = result_msg.get("result", {})

        # MCP tools signal errors via result.isError instead of top-level error key
        if isinstance(result, dict) and result.get("isError"):
            content = result.get("content", [])
            error_text = " ".join(
                b.get("text", "") for b in content if b.get("type") == "text"
            )
            raise MCPError(-1, error_text or "MCP tool error (isError=true)")

        return result

    def _notify(self, method: str, params: dict | None = None) -> None:
        """One-way notification — no id, no response expected."""
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        try:
            self._http.post(self._url, json=payload, headers=self._headers())
        except Exception:
            pass

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

def _read_sse_stream(resp: httpx.Response) -> dict:
    """
    Read an SSE stream line-by-line until a data: line arrives.

    The Playwright MCP server returns HTTP 200 with text/event-stream
    immediately, then sends the actual data line after processing completes.
    We must stream line-by-line rather than reading resp.text upfront.
    """
    for line in resp.iter_lines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if data:
                try:
                    return json.loads(data)
                except json.JSONDecodeError as exc:
                    raise MCPError(-1, f"Invalid JSON in SSE data: {data[:200]}") from exc
    # Stream ended with no data line (e.g. notification ACK)
    return {}


def extract_screenshot(content_blocks: list[dict]) -> bytes | None:
    for block in content_blocks:
        if block.get("type") == "image":
            return base64.b64decode(block["data"])
    return None
