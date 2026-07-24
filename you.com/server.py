#!/usr/bin/env python3
"""
Real-time podcast fact detector — backend.

Serves the front-end UI and proxies fact-check requests to the You.com API,
so the API key never touches the browser and CORS is a non-issue.

Runs on the Python standard library only — no pip install required.

Usage:
    export YDC_API_KEY="your-you.com-api-key"
    python3 server.py
    # open http://localhost:8000
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
API_KEY = os.environ.get("YDC_API_KEY", "").strip()
PORT = int(os.environ.get("PORT", "8000"))

RESEARCH_URL = "https://api.you.com/v1/research"
SEARCH_URL = "https://ydc-index.io/v1/search"
MCP_URL = "https://api.you.com/mcp"

# api.you.com sits behind Cloudflare, which 403s the default Python-urllib
# User-Agent (error 1010). Present a normal browser UA on every outbound call.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Ask the Research API to lead with a machine-parseable verdict so the UI can
# render a coloured badge, followed by a short human explanation.
FACTCHECK_PROMPT = (
    "You are a real-time fact checker for a live podcast. Fact-check the "
    "following spoken statement. Your response MUST begin with exactly one "
    "line of the form 'VERDICT: X' where X is one of TRUE, FALSE, MISLEADING, "
    "or UNVERIFIED. On the next line give one concise sentence (max 30 words) "
    "explaining the verdict. Base the verdict only on reliable sources.\n\n"
    'Statement: "{claim}"'
)


def call_research(claim: str) -> dict:
    """Grounded verdict via the You.com Research API (slower, higher quality)."""
    payload = json.dumps(
        {
            "input": FACTCHECK_PROMPT.format(claim=claim),
            "research_effort": "standard",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        RESEARCH_URL,
        data=payload,
        headers={
            "X-API-Key": API_KEY,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    output = data.get("output", {}) or {}
    content = output.get("content", "") or ""
    sources = output.get("sources", []) or []
    verdict, explanation = _parse_verdict(content)
    return {
        "mode": "research",
        "verdict": verdict,
        "explanation": explanation,
        "content": content,
        "sources": [
            {
                "url": s.get("url", ""),
                "title": s.get("title", "") or s.get("url", ""),
            }
            for s in sources[:5]
        ],
    }


def call_mcp(claim: str) -> dict:
    """Grounded verdict via the You.com MCP server (you-research tool).

    The MCP HTTP endpoint is stateless here (no session id returned on
    initialize), so a single tools/call request is enough. Responses come back
    as JSON, but we also tolerate an SSE-framed body just in case.
    """
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "you-research",
                "arguments": {
                    "input": FACTCHECK_PROMPT.format(claim=claim),
                    "research_effort": "standard",
                },
            },
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        MCP_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")

    envelope = _parse_jsonrpc(raw)
    if "error" in envelope:
        raise RuntimeError(envelope["error"].get("message", "MCP error"))

    result = envelope.get("result", {}) or {}
    parts = result.get("content", []) or []
    content = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text") or ""
    if result.get("isError"):
        raise RuntimeError(content or "MCP tool returned an error")

    verdict, explanation = _parse_verdict(content)
    return {
        "mode": "mcp",
        "verdict": verdict,
        "explanation": explanation,
        "content": content,
        "sources": _sources_from_markdown(content),
    }


def _parse_jsonrpc(raw: str) -> dict:
    """Return the JSON-RPC envelope from a JSON or SSE-framed response body."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # SSE framing: pick the last 'data:' line that parses as JSON.
    envelope = {}
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            try:
                envelope = json.loads(chunk)
            except json.JSONDecodeError:
                continue
    return envelope


def _sources_from_markdown(content: str) -> list:
    """Extract bare URLs from the research markdown as source links."""
    import re

    seen, out = set(), []
    for url in re.findall(r"https?://[^\s\)\]\">]+", content):
        url = url.rstrip(".,);]")
        if url not in seen:
            seen.add(url)
            out.append({"url": url, "title": url})
        if len(out) >= 5:
            break
    return out


def call_search(claim: str) -> dict:
    """Fast evidence via the You.com Web Search API (no synthesized verdict)."""
    qs = urllib.parse.urlencode({"query": claim, "count": 5})
    req = urllib.request.Request(
        f"{SEARCH_URL}?{qs}",
        headers={"X-API-Key": API_KEY, "User-Agent": USER_AGENT},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    # Search API shape: {"results": {"web": [...]}}; fall back to older shapes.
    block = data.get("results", data)
    results = block.get("web", []) if isinstance(block, dict) else []
    if not results:
        results = data.get("web", []) or []
    snippets = []
    sources = []
    for r in results[:5]:
        sources.append({"url": r.get("url", ""), "title": r.get("title", "") or r.get("url", "")})
        snips = r.get("snippets") or ([r["description"]] if r.get("description") else [])
        snippets.extend(snips[:1])
    return {
        "mode": "search",
        "verdict": "UNVERIFIED",
        "explanation": " ".join(snippets[:2])[:280] or "See sources below.",
        "content": "\n".join(f"- {s}" for s in snippets),
        "sources": sources,
    }


def _parse_verdict(content: str):
    """Pull the leading 'VERDICT: X' line out of the model's answer."""
    verdict = "UNVERIFIED"
    explanation = content.strip()
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("VERDICT"):
            for cand in ("TRUE", "FALSE", "MISLEADING", "UNVERIFIED"):
                if cand in upper:
                    verdict = cand
                    break
            # Everything after the verdict line becomes the explanation.
            rest = content.split(line, 1)[-1].strip()
            explanation = rest or explanation
            break
    # Keep the explanation to a tidy single paragraph.
    explanation = " ".join(explanation.split())
    if len(explanation) > 400:
        explanation = explanation[:397] + "..."
    return verdict, explanation


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path == "/health":
            return self._send_json(200, {"ok": True, "has_key": bool(API_KEY)})
        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/api/factcheck":
            return self._send_json(404, {"error": "not found"})

        if not API_KEY:
            return self._send_json(
                500,
                {"error": "YDC_API_KEY is not set on the server. Export it and restart."},
            )

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send_json(400, {"error": "invalid JSON body"})

        claim = (payload.get("claim") or "").strip()
        mode = (payload.get("mode") or "research").strip().lower()
        if not claim:
            return self._send_json(400, {"error": "missing 'claim'"})

        dispatch = {"search": call_search, "mcp": call_mcp, "research": call_research}
        try:
            result = dispatch.get(mode, call_research)(claim)
            result["claim"] = claim
            return self._send_json(200, result)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            return self._send_json(502, {"error": f"You.com API error {e.code}", "detail": detail})
        except urllib.error.URLError as e:
            return self._send_json(502, {"error": f"network error: {e.reason}"})
        except Exception as e:  # noqa: BLE001 — surface anything to the client for the demo
            return self._send_json(500, {"error": str(e)})

    def _serve_file(self, name, content_type):
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                body = f.read()
        except FileNotFoundError:
            return self._send_json(404, {"error": f"{name} not found"})
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")


def main():
    if not API_KEY:
        print("\n  WARNING: YDC_API_KEY is not set — fact-checks will fail.")
        print("  Set it with:  export YDC_API_KEY='your-key'  then restart.\n")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"  Podcast fact detector running →  http://localhost:{PORT}")
    print("  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
