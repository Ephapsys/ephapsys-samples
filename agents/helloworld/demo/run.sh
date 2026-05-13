#!/usr/bin/env bash
# Helloworld A2A demo — full trust story across three agents.
#
#   ./demo/run.sh             open a tmux session with 4 panes (recommended)
#   ./demo/run.sh --no-tmux   print copy-paste commands instead
#
# Prereqs: helloworld-{a,b,c}/.ephapsys_state/ provisioned via quickstart.sh
# in each personalized dir. See ./demo/README.md.

set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELLOWORLD_DIR="$(dirname "$DEMO_DIR")"
AGENTS_DIR="$(dirname "$HELLOWORLD_DIR")"
SCENES_DIR="$DEMO_DIR/scenes"

# shellcheck source=lib.sh
source "$DEMO_DIR/lib.sh"

# ── Parse flags ─────────────────────────────────────────────────
USE_TMUX=1
DRIVER_LOOP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-tmux) USE_TMUX=0; shift ;;
    --__driver-loop) DRIVER_LOOP=1; shift ;;     # internal: invoked inside the tmux driver pane
    -h|--help)
      printf "Usage: %s [--no-tmux]\n" "$0"
      exit 0
      ;;
    *) err "Unknown flag: $1"; exit 2 ;;
  esac
done

# ── Banner + preflight ──────────────────────────────────────────
if (( DRIVER_LOOP == 0 )); then
  clear
  banner
fi

# shellcheck source=scenes/00_setup.sh
source "$SCENES_DIR/00_setup.sh"
if (( DRIVER_LOOP == 0 )); then
  if ! run_scene_setup "$AGENTS_DIR"; then
    exit 1
  fi
fi

# Resolve each agent's instance DID once. The driver uses these for
# /ask and @<ref> commands. a2a_peer.py accepts DIDs and resolves them
# to public_ids internally.
A_DID="$(agent_did "$AGENTS_DIR/helloworld-a")"
B_DID="$(agent_did "$AGENTS_DIR/helloworld-b")"
C_DID="$(agent_did "$AGENTS_DIR/helloworld-c")"

if (( DRIVER_LOOP == 0 )); then
  success "Agents resolved"
  printf "    ${DIM}A: %s${RESET}\n" "$A_DID"
  printf "    ${DIM}B: %s${RESET}\n" "$B_DID"
  printf "    ${DIM}C: %s${RESET}\n" "$C_DID"
fi

# Source scene scripts so run_scene_* functions are in scope.
# shellcheck source=scenes/01_basic_chat.sh
source "$SCENES_DIR/01_basic_chat.sh"
# shellcheck source=scenes/02_prompt_serving.sh
source "$SCENES_DIR/02_prompt_serving.sh"
# shellcheck source=scenes/03_guardrail.sh
source "$SCENES_DIR/03_guardrail.sh"
# shellcheck source=scenes/04_isolation.sh
source "$SCENES_DIR/04_isolation.sh"
# shellcheck source=scenes/05_journal.sh
source "$SCENES_DIR/05_journal.sh"

# Export for scenes that need the workspace root.
export AGENTS_DIR

# ── Mode dispatch ───────────────────────────────────────────────
if (( DRIVER_LOOP == 1 )); then
  run_driver_loop
  exit 0
fi

if (( USE_TMUX == 1 )); then
  if ! command -v tmux >/dev/null 2>&1; then
    warn "tmux is not installed; falling back to --no-tmux mode."
    USE_TMUX=0
  fi
fi

if (( USE_TMUX == 1 )); then
  run_tmux_mode
else
  run_manual_mode
fi
