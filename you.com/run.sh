#!/usr/bin/env bash
# Launch the LiveCheck podcast fact detector.

# Load environment variables from .env file if it exists
if [ -f "$(dirname "$0")/.env" ]; then
  export $(grep -v '^#' "$(dirname "$0")/.env" | xargs)
fi

export PORT="${PORT:-8000}"
echo "Starting LiveCheck on http://localhost:$PORT  (open in Chrome or Edge)"
python3 "$(dirname "$0")/server.py"
