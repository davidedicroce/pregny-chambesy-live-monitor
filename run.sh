#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f .env ]]; then
  echo 'Missing .env: cp .env.example .env; edit as needed' >&2
  exit 1
fi
set -a
# NOTE: .env is a trusted local config file containing shell declarations.
source .env
set +a
exec python3 -u monitor.py "$@"
