#!/usr/bin/env bash
# Multi-peer provisioner for the helloworld A2A demo.
#
# Standalone usage (after helloworld/.env has MODEL_TEMPLATE_ID +
# AGENT_TEMPLATE_ID set, i.e. after quickstart's push step has run):
#
#   ./demo/setup.sh                  # provision a, b, c (default)
#   ./demo/setup.sh --peers 5        # provision a..e
#   ./demo/setup.sh --warmup b       # which peer pre-loads the model (default: b)
#   ./demo/setup.sh --fresh          # remove existing helloworld-* state, re-provision
#
# Normally invoked as part of `./quickstart.sh --demo`, but runnable on
# its own when peers were partially provisioned and you want to recover.

set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELLOWORLD_DIR="$(dirname "$DEMO_DIR")"
AGENTS_DIR="$(dirname "$HELLOWORLD_DIR")"

# shellcheck source=lib.sh
source "$DEMO_DIR/lib.sh"

# ── Args ────────────────────────────────────────────────────────
PEERS=3
WARMUP_LETTER="b"
FRESH=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --peers)  PEERS="$2"; shift 2 ;;
    --warmup) WARMUP_LETTER="$2"; shift 2 ;;
    --fresh)  FRESH=1; shift ;;
    -h|--help)
      printf "Usage: %s [--peers N] [--warmup <letter>] [--fresh]\n" "$0"
      exit 0
      ;;
    *) err "Unknown flag: $1"; exit 2 ;;
  esac
done

if (( PEERS < 2 || PEERS > 7 )); then
  err "PEERS must be between 2 and 7 (got $PEERS)"
  exit 2
fi

LETTERS=(a b c d e f g)
TARGET_LETTERS=("${LETTERS[@]:0:$PEERS}")

# ── Preconditions ───────────────────────────────────────────────
if [[ ! -f "$HELLOWORLD_DIR/.env" ]]; then
  err "$HELLOWORLD_DIR/.env not found. Run quickstart.sh first to bootstrap."
  exit 1
fi

# Source helloworld/.env to get template IDs.
set -a
# shellcheck disable=SC1091
source "$HELLOWORLD_DIR/.env"
set +a

if [[ -z "${MODEL_TEMPLATE_ID:-}" || -z "${AGENT_TEMPLATE_ID:-}" ]]; then
  err "MODEL_TEMPLATE_ID / AGENT_TEMPLATE_ID not set in $HELLOWORLD_DIR/.env"
  err "Run ./quickstart.sh (without --demo) once to bootstrap templates, then retry."
  exit 1
fi

# A2A token is what a2a_peer.py uses for the /agents DID-resolution lookup.
# Without it the SDK falls back to AOC_MODULATION_TOKEN, which lacks /agents
# scope, and every pane dies with a 401 traceback before the demo can start.
if [[ -z "${AOC_A2A_TOKEN:-}" ]]; then
  err "AOC_A2A_TOKEN is not set in $HELLOWORLD_DIR/.env"
  err "Uncomment AOC_A2A_TOKEN in helloworld/.env (or generate one in the AOC console under tokens)."
  exit 1
fi

# Validate warmup letter is in the target set.
case " ${TARGET_LETTERS[*]} " in
  *" $WARMUP_LETTER "*) ;;
  *)
    err "Warmup letter '$WARMUP_LETTER' not in target peers: ${TARGET_LETTERS[*]}"
    exit 2
    ;;
esac

# ── Provision each peer ─────────────────────────────────────────
narrate "Provisioning ${BOLD}${#TARGET_LETTERS[@]}${RESET} peer instances: ${TARGET_LETTERS[*]}"
narrate "Templates ready (model=${DIM}${MODEL_TEMPLATE_ID}${RESET}, agent=${DIM}${AGENT_TEMPLATE_ID}${RESET})"
printf "\n"

for letter in "${TARGET_LETTERS[@]}"; do
  peer_dir="$AGENTS_DIR/helloworld-$letter"

  printf "  ${GOLD}[%s]${RESET} %s\n" "$letter" "$peer_dir"

  # Fresh: nuke the peer dir entirely.
  if (( FRESH == 1 )) && [[ -d "$peer_dir" ]]; then
    info "    removing existing dir (--fresh)"
    rm -rf "$peer_dir"
  fi

  # Create from template if missing.
  if [[ ! -d "$peer_dir" ]]; then
    info "    creating from helloworld template"
    cp -r "$HELLOWORLD_DIR" "$peer_dir"
    # Strip parent state — the venv and any partial state must be local.
    rm -rf "$peer_dir/.ephapsys_state" "$peer_dir/.venv" "$peer_dir/__pycache__"
    # Remove the demo dir from the copy — peers don't need their own demo/.
    rm -rf "$peer_dir/demo"
  fi

  # Always sync .env from helloworld/.env so template IDs are current.
  cp "$HELLOWORLD_DIR/.env" "$peer_dir/.env"

  # Skip if already personalized.
  if [[ -f "$peer_dir/.ephapsys_state/agent_id" ]]; then
    success "    already personalized (agent_id present)"
    continue
  fi

  info "    ensuring venv (one-time, ~30s on first install)…"
  (
    cd "$peer_dir"
    # run_local.sh in 'check' mode creates .venv, installs SDK, runs preflight, exits.
    # Output is noisy; gate behind /dev/null but show errors.
    if ! ./run_local.sh check >/dev/null 2>"$peer_dir/.demo_setup.log"; then
      err "    venv setup failed; see $peer_dir/.demo_setup.log"
      exit 1
    fi
  )

  info "    personalizing instance…"
  (
    cd "$peer_dir"
    # The peer's venv has the SDK + python; use it directly.
    .venv/bin/python "$DEMO_DIR/personalize_peer.py" 2>&1 | sed 's/^/    /'
  )

  if [[ ! -f "$peer_dir/.ephapsys_state/agent_id" ]]; then
    err "    personalization did not produce agent_id; check the SDK output above"
    exit 1
  fi
  success "    personalized"
done

# ── Pre-warm B's model ──────────────────────────────────────────
warmup_dir="$AGENTS_DIR/helloworld-$WARMUP_LETTER"
warmup_marker="$warmup_dir/.ephapsys_state/cache"

printf "\n"
narrate "Pre-warming model on ${BOLD}helloworld-${WARMUP_LETTER}${RESET} (real-inference target)"
narrate "Other peers run in stub mode and don't need the model loaded."

# Skip warmup if the cache dir for this agent already has artifacts.
if [[ -d "$warmup_marker" ]] && find "$warmup_marker" -name "*.safetensors" -size +1M 2>/dev/null | head -1 | grep -q .; then
  success "    model artifacts already cached"
else
  info "    downloading artifacts (one-time, ~1-3 min depending on network)…"
  (
    cd "$warmup_dir"
    SETUP_PREPARE_RUNTIME=1 .venv/bin/python "$DEMO_DIR/personalize_peer.py" 2>&1 | sed 's/^/    /'
  )
  success "    warmup complete"
fi

printf "\n"
success "All ${#TARGET_LETTERS[@]} peers ready. Run ${BOLD}./demo/run.sh${RESET} to start the demo."
