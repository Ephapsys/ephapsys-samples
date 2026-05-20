#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
#  run_lambda.sh — run the HelloWorld agent on Lambda Cloud
# ════════════════════════════════════════════════════════════════
#
# Provisions a Lambda Cloud GPU instance, copies the HelloWorld agent
# code + .env onto it, installs the SDK, and launches the agent inside
# a detached `tmux` session so it survives SSH disconnect. tmux is the
# same multiplexer the A2A demo uses (see demo/lib.sh) — keeping the
# runtime consistent with the demo's UX.
#
# UNLIKE modulation: the VM is NOT auto-terminated on script exit.
# The agent must outlive this script, and on failure the VM is left up
# so the user can SSH in and debug. The user is responsible for tearing
# the VM down when finished — the footer (or the error-path termination
# hint) prints the exact command.
#
# Required env (from .env):
#   AOC_BASE_URL              AOC backend
#   AOC_ORG_ID                Org id (org_…)
#   AOC_PROVISIONING_TOKEN    Bootstrap token (boot_…)
#   AGENT_TEMPLATE_ID         Agent template id from AOC (set by push.sh)
#
# Required env (from .env.lambda):
#   LAMBDA_API_KEY            Lambda Cloud API key (secret_api_…)
#   LAMBDA_SSH_KEY_NAME       Registered SSH key name in Lambda dashboard
#   LAMBDA_SSH_KEY_PATH       Local path to the matching .pem
#
# Optional:
#   LAMBDA_RUNTIME_INSTANCE_TYPES   A10-first fallback list (default below)
#   LAMBDA_API_BASE                 API base override
#   LAMBDA_ATTACH_INSTANCE          Existing instance id to reuse (alt. to --attach)
#
# Flags:
#   --attach <instance_id>          Reuse an existing Lambda instance instead
#                                   of provisioning a new one. Useful for
#                                   recovering from a failed run, or BYO VM.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BLUE="\033[36m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[91m"
RESET="\033[0m"

info()    { printf "${BLUE}[INFO]${RESET} %s\n" "$*"; }
success() { printf "${GREEN}[OK]${RESET} %s\n" "$*"; }
warn()    { printf "${YELLOW}[WARN]${RESET} %s\n" "$*" >&2; }
error()   { printf "${RED}[ERROR]${RESET} %s\n" "$*" >&2; }

# ── Parse args ──────────────────────────────────────────────────
ATTACH_INSTANCE_ID="${LAMBDA_ATTACH_INSTANCE:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --attach)
      ATTACH_INSTANCE_ID="${2:-}"
      [ -n "$ATTACH_INSTANCE_ID" ] || { error "--attach requires an instance id"; exit 1; }
      shift 2
      ;;
    --attach=*)
      ATTACH_INSTANCE_ID="${1#--attach=}"
      shift
      ;;
    -h|--help)
      cat <<EOF
Usage: ./run_lambda.sh [--attach <instance_id>]

Provisions a new Lambda Cloud VM and runs the HelloWorld agent on it.

Options:
  --attach <id>   Reuse an existing instance instead of launching a new one.
                  Useful for: (a) resuming after a failed run where the VM
                  is still up; (b) using a manually-provisioned VM.
                  Can also be set via LAMBDA_ATTACH_INSTANCE env var.
EOF
      exit 0
      ;;
    *)
      error "Unknown argument: $1"
      echo "    See ./run_lambda.sh --help"
      exit 1
      ;;
  esac
done

# ── Load .env and .env.lambda ───────────────────────────────────
if [ ! -f ".env" ]; then
  error "Missing .env — run ./push.sh first (or copy .env.example to .env and fill it in)."
  exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
if [ ! -f ".env.lambda" ]; then
  set +a
  error "Missing .env.lambda — copy .env.lambda.example and fill in LAMBDA_API_KEY, LAMBDA_SSH_KEY_NAME, LAMBDA_SSH_KEY_PATH."
  exit 1
fi
# shellcheck disable=SC1091
source .env.lambda
set +a

# ── Resolve runtime config ──────────────────────────────────────
AGENT_ID="${AGENT_TEMPLATE_ID:-}"
ORG_ID="${AOC_ORG_ID:-}"
BOOTSTRAP_TOKEN="${AOC_PROVISIONING_TOKEN:-}"
BASE_URL="${AOC_BASE_URL:-${AOC_API_URL:-${AOC_API_BASE:-${AOC_API:-}}}}"

# Runtime VM defaults to A10-first (cheaper, sufficient for inference).
# Modulation uses LAMBDA_INSTANCE_TYPES (A100-first); kept separate so
# `push.sh --lambda` and `run.sh --lambda` don't share a list optimized
# for the wrong workload.
LAMBDA_RUNTIME_INSTANCE_TYPES="${LAMBDA_RUNTIME_INSTANCE_TYPES:-gpu_1x_a10,gpu_1x_a100,gpu_1x_a100_sxm4,gpu_1x_h100_pcie,gpu_1x_h100_sxm5,gpu_2x_h100_sxm5}"
REMOTE_BASE_DIR="${REMOTE_BASE_DIR:-~/helloworld}"
TMUX_SESSION="${TMUX_SESSION:-helloworld}"

# ── Validate creds ──────────────────────────────────────────────
if [ -z "$AGENT_ID" ]; then
  error "Missing AGENT_TEMPLATE_ID — run ./push.sh --lambda first to create the agent template."
  exit 1
fi
if [ -z "$ORG_ID" ] || [ -z "$BOOTSTRAP_TOKEN" ] || [ -z "$BASE_URL" ]; then
  error "Missing AOC creds — set AOC_BASE_URL, AOC_ORG_ID, AOC_PROVISIONING_TOKEN in .env."
  exit 1
fi
if [ -z "${LAMBDA_API_KEY:-}" ] || [ -z "${LAMBDA_SSH_KEY_NAME:-}" ] || [ -z "${LAMBDA_SSH_KEY_PATH:-}" ]; then
  error "Missing Lambda creds — set LAMBDA_API_KEY, LAMBDA_SSH_KEY_NAME, LAMBDA_SSH_KEY_PATH in .env.lambda."
  echo "    Generate API key: https://cloud.lambdalabs.com/api-keys"
  exit 1
fi
if [ ! -f "$LAMBDA_SSH_KEY_PATH" ]; then
  error "SSH key not found at: $LAMBDA_SSH_KEY_PATH"
  exit 1
fi
command -v jq >/dev/null || { error "jq not installed"; exit 1; }

# ── Source shared Lambda helpers ────────────────────────────────
LIB_LAMBDA="$SCRIPT_DIR/../../modulators/lib/lambda.sh"
if [ ! -f "$LIB_LAMBDA" ]; then
  error "Shared lib not found at $LIB_LAMBDA"
  exit 1
fi
# shellcheck disable=SC1091
source "$LIB_LAMBDA"

# ── Cost warning ────────────────────────────────────────────────
if [ -n "$ATTACH_INSTANCE_ID" ]; then
  info "Attach mode: reusing existing instance ${ATTACH_INSTANCE_ID}."
  info "This script will NOT terminate the VM on exit; the VM is yours to manage."
else
  warn "Lambda Cloud bills by the hour — VM stays up after this script exits."
  warn "Termination command is printed in the footer; please don't forget."
fi
echo

# ── Launch or attach ────────────────────────────────────────────
# LAMBDA_PRE_EXISTING_VM=1 means the user gave us the VM (via --attach or
# LAMBDA_ATTACH_INSTANCE). The footer suppresses the termination command
# in that case — destroying a VM the user pre-launched would be surprising.
LAMBDA_PRE_EXISTING_VM=0
if [ -n "$ATTACH_INSTANCE_ID" ]; then
  if ! lambda_fetch_instance "$ATTACH_INSTANCE_ID"; then
    error "Could not attach to instance $ATTACH_INSTANCE_ID."
    exit 1
  fi
  LAMBDA_PRE_EXISTING_VM=1
else
  LAMBDA_INSTANCE_NAME="${LAMBDA_INSTANCE_NAME:-ephapsys-helloworld-runtime-$(date +%Y%m%d-%H%M%S)}"
  if ! lambda_launch_instance "$LAMBDA_RUNTIME_INSTANCE_TYPES" "LAMBDA_RUNTIME_INSTANCE_TYPES" "$LAMBDA_INSTANCE_NAME"; then
    error "Could not launch any Lambda instance — all configured types/regions exhausted."
    exit 1
  fi
fi
INSTANCE_ID="$LAMBDA_INSTANCE_ID"
LAUNCHED_TYPE="$LAMBDA_LAUNCHED_TYPE"
LAUNCHED_REGION="$LAMBDA_LAUNCHED_REGION"

# The runtime VM is intentionally persistent — we do NOT trap-terminate it
# on script exit. On failure, leaving the VM up lets the user SSH in and
# diagnose what went wrong (apt failure, network glitch, etc.). The cost
# of a few minutes of A10 time is cheap insurance for keeping the debug
# state available. `print_termination_hint` reminds the user how to clean
# up if they don't want to debug.
print_termination_hint() {
  [ -n "$INSTANCE_ID" ] || return 0
  if [ "${LAMBDA_PRE_EXISTING_VM:-0}" = "1" ]; then
    printf "\n${BLUE}ℹ  Attached VM %s (%s in %s) is yours — left running as expected.${RESET}\n" \
      "$INSTANCE_ID" "$LAUNCHED_TYPE" "$LAUNCHED_REGION" >&2
    if [ -n "${HOST:-}" ]; then
      printf "    SSH in to debug:\n" >&2
      printf "      ssh -i %s ubuntu@%s\n" "$LAMBDA_SSH_KEY_PATH" "$HOST" >&2
    fi
    printf "    Retry with: ./run.sh --lambda --attach %s\n" "$INSTANCE_ID" >&2
    return 0
  fi
  printf "\n${YELLOW}⚠  Lambda VM left running: %s (%s in %s).${RESET}\n" \
    "$INSTANCE_ID" "$LAUNCHED_TYPE" "$LAUNCHED_REGION" >&2
  if [ -n "${HOST:-}" ]; then
    printf "    SSH in to debug:\n" >&2
    printf "      ssh -i %s ubuntu@%s\n" "$LAMBDA_SSH_KEY_PATH" "$HOST" >&2
    printf "    Or resume with: ./run.sh --lambda --attach %s\n" "$INSTANCE_ID" >&2
  fi
  printf "    Or terminate the VM:\n" >&2
  printf "      curl -sS -u \"\$LAMBDA_API_KEY:\" -X POST \\\\\n" >&2
  printf "        %s/instance-operations/terminate \\\\\n" \
    "${LAMBDA_API_BASE:-https://cloud.lambdalabs.com/api/v1}" >&2
  printf "        -H 'Content-Type: application/json' \\\\\n" >&2
  printf "        -d '{\"instance_ids\":[\"%s\"]}'\n" "$INSTANCE_ID" >&2
  printf "      # or via the dashboard: https://cloud.lambdalabs.com/instances\n" >&2
}

# ── Wait active + SSH ───────────────────────────────────────────
if ! lambda_wait_active "$INSTANCE_ID"; then
  error "Instance never became active"
  print_termination_hint
  exit 1
fi
HOST="$LAMBDA_HOST"
success "Instance active at $HOST"

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$LAMBDA_SSH_KEY_PATH")
SSH_CMD=(ssh "${SSH_OPTS[@]}" "ubuntu@${HOST}")
SCP_CMD=(scp "${SSH_OPTS[@]}")

lambda_wait_ssh "$HOST" "$LAMBDA_SSH_KEY_PATH"

# ── Verify GPU + apt deps ───────────────────────────────────────
info "Verifying GPU and installing apt deps..."
"${SSH_CMD[@]}" 'nvidia-smi --query-gpu=name --format=csv,noheader; python3 --version'
"${SSH_CMD[@]}" 'sudo apt-get update -qq >/dev/null 2>&1 && sudo apt-get install -qq -y python3-venv tmux >/dev/null 2>&1; echo done'

# ── Stage agent code + .env to instance ─────────────────────────
info "Copying agent code to instance..."
"${SSH_CMD[@]}" "mkdir -p $REMOTE_BASE_DIR"
"${SCP_CMD[@]}" -C "$SCRIPT_DIR/helloworld_agent.py" "ubuntu@${HOST}:$REMOTE_BASE_DIR/" >/dev/null
"${SCP_CMD[@]}" -C "$SCRIPT_DIR/.env" "ubuntu@${HOST}:$REMOTE_BASE_DIR/.env" >/dev/null

# ── Build remote setup script ───────────────────────────────────
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

SDK_PACKAGE_SOURCE="${HELLOWORLD_SDK_PACKAGE_SOURCE:-${SDK_PACKAGE_SOURCE:-pypi}}"
SDK_EXTRAS="${HELLOWORLD_SDK_EXTRAS:-modulation}"
SDK_VERSION="${HELLOWORLD_SDK_VERSION:-${SDK_VERSION:-}}"
SDK_SPEC="ephapsys[${SDK_EXTRAS}]"
[ -n "$SDK_VERSION" ] && SDK_SPEC="${SDK_SPEC}==${SDK_VERSION}"

REMOTE_SETUP="$TEMP_DIR/setup.sh"
cat > "$REMOTE_SETUP" <<SETUP_EOF
#!/usr/bin/env bash
set -e
cd ${REMOTE_BASE_DIR}
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
# Match modulator's torch pin — Lambda Stack ships CUDA 12.x drivers.
python -m pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124 >/dev/null
SETUP_EOF

case "$(printf '%s' "$SDK_PACKAGE_SOURCE" | tr '[:upper:]' '[:lower:]')" in
  pypi)
    echo "python -m pip install '${SDK_SPEC}' >/dev/null" >> "$REMOTE_SETUP"
    ;;
  testpypi)
    echo "python -m pip install --extra-index-url https://pypi.org/simple --index-url https://test.pypi.org/simple '${SDK_SPEC}' >/dev/null" >> "$REMOTE_SETUP"
    ;;
  *)
    error "Unsupported SDK_PACKAGE_SOURCE: $SDK_PACKAGE_SOURCE"
    exit 1
    ;;
esac
echo "python -c 'from importlib.metadata import version; print(\"  installed ephapsys\", version(\"ephapsys\"))'" >> "$REMOTE_SETUP"

"${SCP_CMD[@]}" -C "$REMOTE_SETUP" "ubuntu@${HOST}:$REMOTE_BASE_DIR/setup.sh" >/dev/null
info "Installing Python environment on instance (may take 2-4 minutes)..."
"${SSH_CMD[@]}" "bash -l $REMOTE_BASE_DIR/setup.sh"

# ── Build remote launch script ──────────────────────────────────
REMOTE_LAUNCH="$TEMP_DIR/launch.sh"
# tmux doesn't have screen's `-L -Logfile` flag, so we `tee` from inside
# the launch script. The user attaching with `tmux attach` still sees
# live output, AND the same content lands in helloworld.log on disk —
# which is also what `tail -f` in the footer reads from.
cat > "$REMOTE_LAUNCH" <<LAUNCH_EOF
#!/usr/bin/env bash
set -e
cd ${REMOTE_BASE_DIR}
set -a
source .env
set +a
source .venv/bin/activate
python3 helloworld_agent.py 2>&1 | tee -a helloworld.log
LAUNCH_EOF
"${SCP_CMD[@]}" -C "$REMOTE_LAUNCH" "ubuntu@${HOST}:$REMOTE_BASE_DIR/launch.sh" >/dev/null
"${SSH_CMD[@]}" "chmod +x $REMOTE_BASE_DIR/launch.sh"

# ── Launch agent in detached tmux session ──────────────────────
# `tmux new-session -d -s` matches the convention used by demo/lib.sh
# (TMUX_SESSION variable, same `tmux attach -t` pattern in the footer).
info "Starting agent in tmux session '$TMUX_SESSION'..."
# Kill any pre-existing session under the same name so re-runs (including
# --attach against a VM that already has an agent running) are idempotent.
"${SSH_CMD[@]}" "tmux kill-session -t $TMUX_SESSION 2>/dev/null || true"
"${SSH_CMD[@]}" "tmux new-session -d -s $TMUX_SESSION 'bash $REMOTE_BASE_DIR/launch.sh'"
sleep 2
if ! "${SSH_CMD[@]}" "tmux has-session -t $TMUX_SESSION 2>/dev/null"; then
  error "Agent tmux session did not start — pulling log for diagnostics:"
  "${SSH_CMD[@]}" "cat $REMOTE_BASE_DIR/helloworld.log 2>/dev/null || echo '(no log written)'" >&2
  print_termination_hint
  exit 1
fi
success "Agent tmux session is running"

# ── Footer ──────────────────────────────────────────────────────
KEY_PATH_QUOTED="$LAMBDA_SSH_KEY_PATH"
cat <<EOF

${GREEN}✅ HelloWorld agent is running on Lambda Cloud.${RESET}
   Instance: $INSTANCE_ID ($LAUNCHED_TYPE in $LAUNCHED_REGION)
   Host:     ubuntu@$HOST

Stream logs:
  ssh -i $KEY_PATH_QUOTED ubuntu@$HOST 'tail -f $REMOTE_BASE_DIR/helloworld.log'

Attach to the tmux session (Ctrl-B then d to detach):
  ssh -t -i $KEY_PATH_QUOTED ubuntu@$HOST 'tmux attach -t $TMUX_SESSION'

Stop the bot (keeps the VM up):
  ssh -i $KEY_PATH_QUOTED ubuntu@$HOST 'tmux kill-session -t $TMUX_SESSION'

EOF

if [ "$LAMBDA_PRE_EXISTING_VM" = "1" ]; then
  cat <<EOF
${BLUE}ℹ  VM was attached (--attach), not launched by this script.${RESET}
   Termination is your call — only terminate if you're done with the VM:
     curl -sS -u "\$LAMBDA_API_KEY:" -X POST \\
       ${LAMBDA_API_BASE:-https://cloud.lambdalabs.com/api/v1}/instance-operations/terminate \\
       -H 'Content-Type: application/json' \\
       -d '{"instance_ids":["$INSTANCE_ID"]}'

EOF
else
  cat <<EOF
${YELLOW}⚠  TERMINATE THE VM when finished (Lambda is billing by the hour):${RESET}
  curl -sS -u "\$LAMBDA_API_KEY:" -X POST \\
    ${LAMBDA_API_BASE:-https://cloud.lambdalabs.com/api/v1}/instance-operations/terminate \\
    -H 'Content-Type: application/json' \\
    -d '{"instance_ids":["$INSTANCE_ID"]}'
  # or via the dashboard: https://cloud.lambdalabs.com/instances

EOF
fi
