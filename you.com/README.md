# LiveCheck — Real-Time Podcast Fact Detector

Speak into your mic like a podcast host. Your speech is transcribed live, and
factual claims are automatically fact-checked against the web using the
**You.com API** — each check comes back with a **verdict** and **citations**.

Built for the You.com hackathon. Three moving parts:

1. **Speech-to-text** — the browser's Web Speech API (real-time, no setup).
2. **You.com API** — a tiny Python backend proxies claims to the You.com
   Research / Search API (keeps your key secret, avoids CORS).
3. **Podcast video UI** — your webcam is the "podcast video", with live
   captions and a fact-check feed.

## Run it

Requires only Python 3 — **no `pip install` needed** (standard library only).

```bash
export YDC_API_KEY="your-you.com-api-key"   # get one at https://you.com/docs
python3 server.py
```

Open **http://localhost:8000** in **Chrome or Edge** (they support the Web
Speech API), click **● Go Live**, allow the mic + camera, and start talking.

## How it works

- Finalized sentences run through a lightweight **claim detector** (numbers,
  dates, comparatives, is/are assertions) — claim-like sentences are checked
  automatically. You can also force-check the last sentence with the button.
Three interchangeable You.com integrations, switchable in the UI:

- **REST** → `POST https://api.you.com/v1/research`, a grounded, cited answer.
  The backend prompts it to lead with `VERDICT: TRUE|FALSE|MISLEADING|
  UNVERIFIED`, which becomes the badge.
- **MCP** → `POST https://api.you.com/mcp` (JSON-RPC), calling the server's
  `you-research` tool. Same Research engine, reached over the Model Context
  Protocol with `Authorization: Bearer` auth. Source URLs are extracted from
  the answer markdown.
- **Fast** → `GET https://ydc-index.io/v1/search` for quick web evidence
  (no synthesized verdict) when you want lower latency during a live demo.

All requests send a browser `User-Agent` — `api.you.com` sits behind
Cloudflare, which 403s the default `Python-urllib` agent (error 1010).

## Files

| File | Purpose |
|------|---------|
| `server.py` | Stdlib HTTP server: serves the UI + proxies to You.com |
| `index.html` | Podcast UI: video, live captions, fact-check feed |

## Demo tips

- **Chrome/Edge only** for speech recognition. Requires an internet connection.
- To fact-check an actual podcast, play it out loud so the mic picks it up.
- Research mode is higher quality but slower; switch to **Fast** mode for a
  snappier live demo.
- No camera? The UI still works — the stage just shows a gradient.
