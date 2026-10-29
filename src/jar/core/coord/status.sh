#!/bin/bash
# The status board (#1519): see scripts/coord/README.md and status.py. Read-only.
exec python3 "$(cd "$(dirname "$0")" && pwd)/status.py" "$@"
