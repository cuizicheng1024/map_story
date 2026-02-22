#!/bin/bash
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
nohup python "$ROOT/.github/skills/map-story/script/story_map.py" --serve --port 8765 > /tmp/story_map_server.log 2>&1 &
if [ ! -d "$ROOT/node_modules" ]; then
  npm install
fi
nohup npm run dev -- --host 127.0.0.1 --port 5173 > /tmp/story_map_frontend.log 2>&1 &
sleep 1
open "http://localhost:5173"
