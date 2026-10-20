#!/usr/bin/env sh
# One-command entry point: ./run.sh <organisation-number batch file> [output directory]
# Accepts JSON, JSONL or plain-text lists of 9-digit organisation numbers. No install step or API key is required.
set -eu
if [ "$#" -lt 1 ]; then
  echo "usage: ./run.sh <batch.jsonl|.json|.txt> [output-dir]" >&2
  exit 2
fi
BATCH="$1"
OUT="${2:-out}"
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "Python 3.9+ required")'
HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -m signalpost --organisations "$BATCH" --output "$OUT/envelopes.jsonl" --report "$OUT/run-report.json" \
  --site "$OUT/site" --state-dir "${SIGNALPOST_STATE_DIR:-$OUT/state}"
