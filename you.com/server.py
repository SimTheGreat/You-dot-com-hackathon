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

# Load environment variables from .env if present
def _load_env():
    env_path = os.path.join(HERE, ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        os.environ[k] = v
        except Exception as e:
            sys.stderr.write(f"Error loading .env file: {e}\n")

_load_env()
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
    "following spoken statement using live web search evidence.\n\n"
    "EVALUATION MATRIX & TRUST HIERARCHY (SoT Architecture):\n"
    "1. Level 0 (HIGHEST PRIORITY): Institutional, academic (.edu, .gov), peer-reviewed science (PubMed, JSTOR), and primary document files (.pdf).\n"
    "2. Level 1 (HIGH TRUST): Major journalism & wire services (Reuters, AP, BBC, WSJ, Pew Research).\n"
    "3. Level 2 (MODERATE TRUST): Tech/business media (TechCrunch, Forbes, Wired).\n"
    "4. Level 3/4 (LOW TRUST): Social media, forums (Reddit, X), tabloids, personal blogs.\n\n"
    "CRITICAL RULE: Prioritize Level 0 (.edu, .gov, document files) and Level 1 sources above all else. If Level 0/1 sources refute a claim, their verdict OVERRIDES lower-level sources.\n\n"
    "Your response MUST begin with exactly one line of the form:\n"
    "VERDICT: X\n"
    "where X is one of TRUE, FALSE, MISLEADING, or UNVERIFIED.\n"
    "On the next line give one concise sentence (max 30 words) explaining the verdict, highlighting any .edu, .gov, or file evidence if available.\n\n"
    'Statement: "{claim}"'
)


def _classify_source(url: str) -> dict:
    """Classify URL according to SoT_Markdown.md trust hierarchy."""
    import urllib.parse

    parsed = urllib.parse.urlparse(url.lower())
    domain = parsed.netloc
    path = parsed.path

    # Level 0: .edu, .gov, .mil, .int, academic databases, PDF/document files
    is_edu = domain.endswith(".edu") or ".edu." in domain
    is_gov = domain.endswith(".gov") or ".gov." in domain or domain.endswith(".mil") or domain.endswith(".int")
    is_file = any(path.endswith(ext) for ext in [".pdf", ".doc", ".docx", ".csv", ".xls", ".xlsx", ".txt", ".xml"])
    level0_domains = (
        "ncbi.nlm.nih.gov", "pubmed", "jstor.org", "sciencedirect.com", "arxiv.org",
        "ieee.org", "nature.com", "sciencemag.org", "who.int", "cdc.gov", "nasa.gov",
        "nih.gov", "noaa.gov", "stanford.edu", "mit.edu", "harvard.edu", "berkeley.edu"
    )
    is_level0_domain = any(d in domain for d in level0_domains)

    if is_edu or is_gov or is_file or is_level0_domain:
        if is_edu:
            badge = "🎓 .edu Academic"
            label = "Level 0: .edu Academic"
        elif is_file:
            badge = "📄 Document File"
            label = "Level 0: Primary File"
        elif is_gov:
            badge = "🏛️ .gov Institutional"
            label = "Level 0: .gov Institutional"
        else:
            badge = "🔬 Peer-Reviewed Science"
            label = "Level 0: Academic & Scientific"
        return {"level": 0, "label": label, "badge": badge, "is_high_trust": True}

    # Level 1: Wire services & major reputable news
    level1_domains = (
        "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "wsj.com",
        "bloomberg.com", "pewresearch.org", "ft.com", "economist.com",
        "npr.org", "pbs.org", "worldbank.org", "oecd.org", "bls.gov"
    )
    if any(d in domain for d in level1_domains):
        return {"level": 1, "label": "Level 1: Major News & Wire", "badge": "📰 Major Wire / News", "is_high_trust": True}

    # Level 3: Social & User-Generated Content
    level3_domains = (
        "reddit.com", "medium.com", "twitter.com", "x.com", "substack.com",
        "linkedin.com", "quora.com", "tiktok.com", "youtube.com", "facebook.com"
    )
    if any(d in domain for d in level3_domains):
        return {"level": 3, "label": "Level 3: User-Generated / Social", "badge": "💬 User-Generated", "is_high_trust": False}

    # Level 4: Tabloid / Clickbait / High Bias
    level4_domains = ("dailymail.co.uk", "thesun.co.uk", "nationalenquirer.com")
    if any(d in domain for d in level4_domains):
        return {"level": 4, "label": "Level 4: Low Reliability", "badge": "⚠️ Low Reliability", "is_high_trust": False}

    # Default: Level 2 Secondary Media & Tech Publications
    return {"level": 2, "label": "Level 2: Secondary Media", "badge": "🌐 Web Media", "is_high_trust": False}


def _process_sources(raw_sources: list) -> list:
    """Enrich and prioritize sources according to SoT_Markdown.md (Level 0 .edu & files first)."""
    enriched = []
    seen = set()
    for s in raw_sources:
        url = s.get("url", "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        title = s.get("title", "") or url
        cls = _classify_source(url)
        enriched.append({
            "url": url,
            "title": title,
            "level": cls["level"],
            "label": cls["label"],
            "badge": cls["badge"],
            "is_high_trust": cls["is_high_trust"],
        })
    # Sort by trust level (Level 0 is highest authority)
    enriched.sort(key=lambda x: x["level"])
    return enriched


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
    raw_sources = [
        {"url": s.get("url", ""), "title": s.get("title", "") or s.get("url", "")}
        for s in sources
    ]
    processed_sources = _process_sources(raw_sources)[:5]
    return {
        "mode": "research",
        "verdict": verdict,
        "explanation": explanation,
        "content": content,
        "sources": processed_sources,
        "has_level0": any(s["level"] == 0 for s in processed_sources),
    }


def call_mcp(claim: str) -> dict:
    """Grounded verdict via the You.com MCP server (you-research tool)."""
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
    raw_sources = _sources_from_markdown(content)
    processed_sources = _process_sources(raw_sources)[:5]
    return {
        "mode": "mcp",
        "verdict": verdict,
        "explanation": explanation,
        "content": content,
        "sources": processed_sources,
        "has_level0": any(s["level"] == 0 for s in processed_sources),
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

    block = data.get("results", data)
    results = block.get("web", []) if isinstance(block, dict) else []
    if not results:
        results = data.get("web", []) or []
    snippets = []
    raw_sources = []
    for r in results[:5]:
        raw_sources.append({"url": r.get("url", ""), "title": r.get("title", "") or r.get("url", "")})
        snips = r.get("snippets") or ([r["description"]] if r.get("description") else [])
        snippets.extend(snips[:1])
    processed_sources = _process_sources(raw_sources)[:5]
    return {
        "mode": "search",
        "verdict": "UNVERIFIED",
        "explanation": " ".join(snippets[:2])[:280] or "See sources below.",
        "content": "\n".join(f"- {s}" for s in snippets),
        "sources": processed_sources,
        "has_level0": any(s["level"] == 0 for s in processed_sources),
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
