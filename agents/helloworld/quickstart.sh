#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colors & Styles ──────────────────────────────────────────────
BOLD="\033[1m"
DIM="\033[2m"
BLUE="\033[36m"
GREEN="\033[32m"
YELLOW="\033[33m"
GOLD="\033[38;5;220m"
WHITE="\033[97m"
RESET="\033[0m"

# ── Output helpers ───────────────────────────────────────────────
banner() {
  printf "\n"
  printf "${GOLD}"
  printf "    ╔═══════════════════════════════════════════════════╗\n"
  printf "    ║                                                   ║\n"
  printf "    ║   ${WHITE}${BOLD}⚡ Ephapsys HelloWorld Agent Quickstart ⚡${RESET}${GOLD}      ║\n"
  printf "    ║                                                   ║\n"
  printf "    ║   ${RESET}${DIM}Setting up your first trustworthy AI agent${RESET}${GOLD}      ║\n"
  printf "    ║                                                   ║\n"
  printf "    ╚═══════════════════════════════════════════════════╝\n"
  printf "${RESET}\n"
}

info() {
  printf "  ${BLUE}%s${RESET} %b\n" ">" "$*"
}

success() {
  printf "  ${GREEN}%s${RESET} %b\n" "+" "$*"
}

warn() {
  printf "  ${YELLOW}%s${RESET} %b\n" "!" "$*" >&2
}

step() {
  local num="$1"; shift
  printf "\n  ${GOLD}[${num}]${RESET} ${BOLD}%s${RESET}\n" "$*"
}

separator() {
  printf "  ${DIM}%s${RESET}\n" "────────────────────────────────────────────────"
}



# ── First-run .env setup ────────────────────────────────────────
if [[ ! -f ".env" && -f ".env.example" ]]; then
  cp .env.example .env
  banner
  printf "  ${GREEN}+${RESET} Created ${BOLD}.env${RESET} from .env.example\n"
  printf "\n"
  separator
  printf "\n"
  printf "  Before continuing, edit ${BOLD}.env${RESET} and set:\n"
  printf "\n"
  printf "    ${WHITE}${BOLD}AOC_BASE_URL${RESET}            ${DIM}https://api.ephapsys.com${RESET}\n"
  printf "    ${WHITE}${BOLD}AOC_ORG_ID${RESET}              ${DIM}from AOC > Organization${RESET}\n"
  printf "    ${WHITE}${BOLD}AOC_PROVISIONING_TOKEN${RESET}  ${DIM}from AOC > Organization > Tokens (boot_...)${RESET}\n"
  printf "    ${WHITE}${BOLD}AOC_MODULATION_TOKEN${RESET}    ${DIM}from AOC > Organization > Tokens (mod_...)${RESET}\n"
  printf "    ${WHITE}${BOLD}HF_TOKEN${RESET}                ${DIM}only if your model repo is private/gated${RESET}\n"
  printf "\n"
  separator
  printf "\n"
  printf "  ${DIM}New to Ephapsys? Sign up at ${RESET}${BLUE}https://ephapsys.com${RESET}\n"
  printf "\n"
  printf "  Then rerun:\n"
  printf "    ${BOLD}./quickstart.sh${RESET}\n"
  printf "\n"
  exit 0
fi

GLOBAL_START=$SECONDS
clear
banner

# ── Parse args ──────────────────────────────────────────────────
MODE="local"
FRESH_START=false
DEMO=false
DEMO_PEERS=3
ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gcp)        MODE="gcp"; shift ;;
    --local)      MODE="local"; shift ;;
    --lambda)     MODE="lambda"; shift ;;
    --fresh)      FRESH_START=true; shift ;;
    --a2a-demo)       DEMO=true; shift ;;
    --a2a-demo-peers) DEMO=true; DEMO_PEERS="$2"; shift 2 ;;
    *)            ARGS+=("$1"); shift ;;
  esac
done

if $DEMO && [[ "$MODE" == "lambda" ]]; then
  printf "  ${YELLOW}!${RESET} --a2a-demo and --lambda cannot be combined.\n" >&2
  printf "    The A2A peer cluster needs persistent local peers; Lambda is not supported.\n" >&2
  printf "    Use --a2a-demo (local) or --lambda alone.\n" >&2
  exit 1
fi

if $DEMO && [[ "$MODE" != "local" ]]; then
  warn "--a2a-demo currently runs only in local mode; ignoring --gcp."
  MODE="local"
fi

if [[ "$MODE" == "lambda" && ! -f ".env.lambda" && -f ".env.lambda.example" ]]; then
  cp .env.lambda.example .env.lambda
  banner
  printf "  ${GREEN}+${RESET} Created ${BOLD}.env.lambda${RESET} from .env.lambda.example\n"
  printf "\n"
  separator
  printf "\n"
  printf "  Before continuing, edit ${BOLD}.env.lambda${RESET} and set:\n"
  printf "\n"
  printf "    ${WHITE}${BOLD}LAMBDA_API_KEY${RESET}         ${DIM}from https://cloud.lambdalabs.com/api-keys${RESET}\n"
  printf "    ${WHITE}${BOLD}LAMBDA_SSH_KEY_NAME${RESET}    ${DIM}name of an SSH key registered in the Lambda dashboard${RESET}\n"
  printf "    ${WHITE}${BOLD}LAMBDA_SSH_KEY_PATH${RESET}    ${DIM}local path to the matching .pem (chmod 400)${RESET}\n"
  printf "\n"
  separator
  printf "\n"
  printf "  Then rerun:\n"
  printf "    ${BOLD}./quickstart.sh --lambda${RESET}\n"
  printf "\n"
  exit 0
fi

if $FRESH_START; then
  step "0" "Fresh start"
  FRESH_TAG="v$(date +%Y%m%d-%H%M%S)"
  sed -i '' 's/^MODEL_TEMPLATE_ID=.*/MODEL_TEMPLATE_ID=/' .env 2>/dev/null || sed -i 's/^MODEL_TEMPLATE_ID=.*/MODEL_TEMPLATE_ID=/' .env
  sed -i '' 's/^AGENT_TEMPLATE_ID=.*/AGENT_TEMPLATE_ID=/' .env 2>/dev/null || sed -i 's/^AGENT_TEMPLATE_ID=.*/AGENT_TEMPLATE_ID=/' .env
  rm -rf .ephapsys_state .venv ../../modulators/language/.venv 2>/dev/null || true
  if $DEMO; then
    # Also wipe any peer dirs so setup re-creates them.
    for letter in a b c d e f g; do
      rm -rf "../helloworld-${letter}" 2>/dev/null || true
    done
  fi
  export HELLOWORLD_MODEL_NAME="HelloWorld Starter Model ${FRESH_TAG}"
  export AGENT_TEMPLATE_NAME="HelloWorld Agent Template ${FRESH_TAG}"
  success "Cleared state — starting fresh as ${DIM}${FRESH_TAG}${RESET}"
fi

if [[ "$MODE" == "lambda" ]]; then
  info "Mode: ${BOLD}${MODE}${RESET} ${DIM}(modulation on Lambda Cloud, agent runs on persistent Lambda VM)${RESET}"
else
  info "Mode: ${BOLD}${MODE}${RESET}"
fi

# ── Helpers ─────────────────────────────────────────────────────
save_env_var() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" ".env"; then
    sed -i '' "s|^${key}=.*|${key}=${value}|" ".env" 2>/dev/null || sed -i "s|^${key}=.*|${key}=${value}|" ".env"
  else
    printf '\n%s=%s\n' "$key" "$value" >>".env"
  fi
}

resolve_existing_templates() {
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a

  local aoc_api api_token model_repo model_kind model_name agent_label
  aoc_api="${AOC_BASE_URL:-${AOC_API_URL:-${AOC_API_BASE:-${AOC_API:-http://localhost:7001}}}}"
  api_token="${API_TOKEN:-${AOC_MODULATION_TOKEN:-}}"
  model_repo="${HELLOWORLD_MODEL_REPO:-Qwen/Qwen3.5-0.8B}"
  model_kind="${HELLOWORLD_MODEL_KIND:-language}"
  model_name="${HELLOWORLD_MODEL_NAME:-HelloWorld Starter Model}"
  agent_label="${AGENT_TEMPLATE_NAME:-HelloWorld Agent Template}"

  if [[ -n "${MODEL_TEMPLATE_ID:-}" && -n "${AGENT_TEMPLATE_ID:-}" ]]; then
    success "Templates already configured"
    return 0
  fi

  if ! command -v curl >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    warn "curl or jq is missing; skipping template lookup."
    return 1
  fi

  if [[ -z "$api_token" ]]; then
    warn "AOC_MODULATION_TOKEN not set; skipping template lookup."
    return 1
  fi

  local auth_header model_id agent_id
  auth_header=(-H "Authorization: Bearer ${api_token}")

  if [[ -z "${MODEL_TEMPLATE_ID:-}" ]]; then
    model_id="$(
      curl -sS "${auth_header[@]}" "${aoc_api}/models?type=TEMPLATE" | jq -r \
        --arg repo "$model_repo" \
        --arg kind "$model_kind" \
        --arg name "$model_name" '
        (.items // [])
        | map(select((((.model_kind // .kind // "") | ascii_downcase) == ($kind | ascii_downcase))
          and ((.source_repo // "") == $repo or (.name // "") == $name or (.name // "") == ("HuggingFace " + $repo))))
        | sort_by(.created_at // 0)
        | last
        | (.ID // .public_id // .internal_id // ._id // empty)'
    )"
    if [[ -n "$model_id" ]]; then
      success "Found model template: ${DIM}${model_id}${RESET}"
      save_env_var MODEL_TEMPLATE_ID "$model_id"
    fi
  fi

  if [[ -z "${AGENT_TEMPLATE_ID:-}" ]]; then
    agent_id="$(
      curl -sS "${auth_header[@]}" "${aoc_api}/agents?type=TEMPLATE" | jq -r \
        --arg lbl "$agent_label" '
        map(select((.label // "") == $lbl))
        | first
        | (.id // .public_id // .ID // ._id // empty)'
    )"
    if [[ -n "$agent_id" ]]; then
      success "Found agent template: ${DIM}${agent_id}${RESET}"
      save_env_var AGENT_TEMPLATE_ID "$agent_id"
    fi
  fi

  set -a
  # shellcheck disable=SC1091
  source .env
  set +a

  [[ -n "${MODEL_TEMPLATE_ID:-}" && -n "${AGENT_TEMPLATE_ID:-}" ]]
}

# ── Lambda runtime capacity preflight ──────────────────────────
# Modulation on Lambda takes ~30 min and ~$1 of GPU time. If runtime
# capacity (LAMBDA_RUNTIME_INSTANCE_TYPES) is exhausted, run.sh would
# fail at Step 2 — wasting the modulation cost. Probe runtime capacity
# BEFORE Step 1 and bail early so the user can widen the list first.
#
# Best-effort: skipped silently if jq or .env.lambda is missing, or if
# the Lambda API doesn't respond. We never block on infrastructure we
# can't reach — the goal is to catch the common exhausted-capacity case,
# not to be a hard prerequisite.
preflight_lambda_runtime_capacity() {
  local lib="$SCRIPT_DIR/../../modulators/lib/lambda.sh"
  if [ ! -f "$lib" ]; then
    warn "Lambda lib not found at $lib; skipping runtime preflight."
    return 0
  fi
  if [ ! -f "$SCRIPT_DIR/.env.lambda" ]; then
    warn ".env.lambda missing; skipping runtime preflight."
    return 0
  fi
  if ! command -v jq >/dev/null 2>&1; then
    warn "jq not installed; skipping runtime preflight."
    return 0
  fi

  step "0" "Lambda runtime capacity preflight"
  info "Checking runtime capacity before kicking off ~30 min of modulation..."

  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env.lambda"
  set +a
  # shellcheck disable=SC1091
  source "$lib"

  local types_csv="${LAMBDA_RUNTIME_INSTANCE_TYPES:-gpu_1x_a10,gpu_1x_a100,gpu_1x_a100_sxm4,gpu_1x_h100_pcie,gpu_1x_h100_sxm5,gpu_2x_h100_sxm5}"
  local avail
  avail="$(lambda_api GET /instance-types 2>/dev/null || true)"
  if [ -z "$avail" ]; then
    warn "Could not query Lambda /instance-types (LAMBDA_API_KEY missing/invalid?); skipping preflight."
    return 0
  fi

  local found_type="" found_regions=""
  IFS=',' read -ra TYPES <<< "$types_csv"
  for itype in "${TYPES[@]}"; do
    local regions
    regions="$(echo "$avail" | jq -r ".data[\"$itype\"].regions_with_capacity_available[]?.name" 2>/dev/null | paste -sd ',' -)"
    if [ -n "$regions" ]; then
      found_type="$itype"
      found_regions="$regions"
      break
    fi
  done

  if [ -n "$found_type" ]; then
    success "Runtime capacity available: ${found_type} (regions: ${found_regions})"
    return 0
  fi

  printf "\n  ${YELLOW}!${RESET} ${BOLD}No runtime capacity available across LAMBDA_RUNTIME_INSTANCE_TYPES.${RESET}\n" >&2
  printf "    Modulation would burn ~30 min and ~\$1 of GPU time only to fail at Step 2.\n" >&2
  printf "    Aborting before any spend.\n" >&2
  lambda_print_available_capacity "$avail" "LAMBDA_RUNTIME_INSTANCE_TYPES" >&2
  exit 1
}

if [[ "$MODE" == "lambda" ]]; then
  preflight_lambda_runtime_capacity
fi

# ── Step 1: Resolve or bootstrap ────────────────────────────────
if $FRESH_START; then
  step "1" "Registering model and agent templates"
  info "This secures your model in AOC and creates an agent template."
  printf "\n"
  export HELLOWORLD_MODEL_NAME
  export AGENT_TEMPLATE_NAME
  if [[ "$MODE" == "gcp" ]]; then
    ./push.sh --mode gcp --force-register --label "${AGENT_TEMPLATE_NAME}" "${ARGS[@]+"${ARGS[@]}"}"
  elif [[ "$MODE" == "lambda" ]]; then
    ./push.sh --mode lambda --force-register --label "${AGENT_TEMPLATE_NAME}" "${ARGS[@]+"${ARGS[@]}"}"
  else
    ./push.sh --mode local --force-register --label "${AGENT_TEMPLATE_NAME}" "${ARGS[@]+"${ARGS[@]}"}"
  fi
elif ! resolve_existing_templates; then
  step "1" "First-time setup"
  info "Registering models, running modulation, creating agent template."
  info "This may take a few minutes on first run."
  printf "\n"
  if [[ "$MODE" == "gcp" ]]; then
    ./push.sh --mode gcp "${ARGS[@]+"${ARGS[@]}"}"
  elif [[ "$MODE" == "lambda" ]]; then
    ./push.sh --mode lambda "${ARGS[@]+"${ARGS[@]}"}"
  else
    ./push.sh --mode local "${ARGS[@]+"${ARGS[@]}"}"
  fi
else
  step "1" "Templates ready"
fi

# ── Step 2: Launch ──────────────────────────────────────────────
if $DEMO; then
  step "2" "Provisioning ${DEMO_PEERS} demo peers + pre-warming model"
  separator
  ./demo/setup.sh --peers "$DEMO_PEERS"

  step "3" "Launching A2A trust demo"
  separator
  exec ./demo/run.sh
fi

step "2" "Launching agent"
separator

if [[ "$MODE" == "gcp" ]]; then
  ./run.sh --gcp
elif [[ "$MODE" == "lambda" ]]; then
  ./run.sh --lambda
else
  ./run.sh --local
fi
