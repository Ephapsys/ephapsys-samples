#!/usr/bin/env bash
# Lambda-cloud counterpart to modulate_gcp.sh — thin wrapper that
# exports the per-modulator vars and hands off to the common runner.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export MODULATOR_DIR="$SCRIPT_DIR"
export MODULATOR_KIND="language"
export TRAINER_SCRIPT="train_language.py"
export DEFAULT_OUTDIR="./artifacts"

exec "$SCRIPT_DIR/../modulate_lambda_common.sh" "$@"
