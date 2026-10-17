#!/usr/bin/env bash
# Kill persistent bash shells left by killed solvers (shell.pid written by common.py).
set -u
for f in workspaces*/workspaces/*/out/shell.pid; do
  [ -f "$f" ] || continue
  pid=$(cat "$f" 2>/dev/null || true)
  if [ -n "${pid:-}" ]; then
    kill -9 -"$pid" 2>/dev/null || true
  fi
  rm -f "$f" 2>/dev/null || true
done
