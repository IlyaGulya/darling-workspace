#!/bin/sh
# Keep the public CLI; stop analysis and shared evidence reading have one implementation.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
exec python3 -B "$SCRIPT_DIR/../west_commands/stop_reason.py" "$@"
