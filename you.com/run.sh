#!/usr/bin/env bash
# Launch the LiveCheck podcast fact detector.
# NOTE: contains your You.com API key — keep this file private, don't commit/share it.
export YDC_API_KEY="ydc-sk-24a1e4fd19e595ff-To01PzZEf20wnKlKj4d16dkpmxlTZWnt-008baa04"
export PORT="${PORT:-8000}"
echo "Starting LiveCheck on http://localhost:$PORT  (open in Chrome or Edge)"
python3 "$(dirname "$0")/server.py"
