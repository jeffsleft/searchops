#!/bin/bash
# Short wrapper so the long "python3 ... --live" command line can't get
# split by terminal/chat copy-paste line-wrapping. Reads SEARCHOPS_SYNC_TOKEN
# from the environment (must already be exported in this shell) — never
# hardcode the token here.
cd "$(dirname "$0")/.."
python3 scripts/backfill_interview_sessions.py sessions.json --live
