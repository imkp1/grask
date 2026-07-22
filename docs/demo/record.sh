#!/usr/bin/env bash
# Record docs/grask.gif.
#
#   ./docs/demo/record.sh
#
# Needs asciinema (uv tool install asciinema) and agg, the asciinema GIF
# renderer (https://github.com/asciinema/agg/releases). Not vhs: vhs pulls ttyd
# and ffmpeg through Homebrew, which wants newer Command Line Tools than this
# machine has. asciinema is pure Python and agg is a single static binary.
#
# Everything runs against a throwaway GRASK_HOME under this directory, so
# recording never touches the developer's real database. It is reseeded every
# run because answering a probe is permanent — a second take against a used
# database would record an empty queue.
set -euo pipefail
cd "$(dirname "$0")"

AGG=${AGG:-agg}
command -v asciinema >/dev/null || { echo "need asciinema: uv tool install asciinema" >&2; exit 1; }
command -v "$AGG" >/dev/null || { echo "need agg: set AGG=/path/to/agg" >&2; exit 1; }

export GRASK_HOME="$PWD/.demo-home"
cast="$PWD/.demo-home/demo.cast"

rm -rf "$GRASK_HOME"
mkdir -p "$GRASK_HOME"
python3 seed_demo.py

PATH="$PWD/bin:$PATH" asciinema rec --overwrite -c "python3 driver.py" "$cast"

# asciinema records the size of the terminal it was launched from, which is an
# 80x24 fallback under a non-tty. The session actually ran at driver.py's
# COLS x ROWS, so the header is corrected to match before rendering.
python3 - "$cast" <<'PY'
import json, re, sys
cast = sys.argv[1]
src = open("driver.py").read()
cols, rows = map(int, re.search(r"^COLS, ROWS = (\d+), (\d+)", src, re.M).groups())
lines = open(cast).read().splitlines()
header = json.loads(lines[0])
header["width"], header["height"] = cols, rows
open(cast, "w").write(json.dumps(header) + "\n" + "\n".join(lines[1:]) + "\n")
PY

"$AGG" --theme monokai --font-size 18 --line-height 1.4 "$cast" ../grask.gif

rm -rf "$GRASK_HOME"
echo "wrote docs/grask.gif"
