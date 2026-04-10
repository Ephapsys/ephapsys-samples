#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colors ────────────────────────────────────────────────────────
BOLD="\033[1m"
DIM="\033[2m"
BLUE="\033[36m"
GREEN="\033[32m"
YELLOW="\033[33m"
MAGENTA="\033[35m"
GOLD="\033[38;5;220m"
WHITE="\033[37m"
RESET="\033[0m"

# ── Output helpers ────────────────────────────────────────────────
banner() {
  printf "\n"
  printf "${GOLD}  ╭──────────────────────────────────────────╮${RESET}\n"
  printf "${GOLD}  │${RESET}${BOLD}   Ephapsys HelloWorld Agent Quickstart   ${RESET}${GOLD}│${RESET}\n"
  printf "${GOLD}  ╰──────────────────────────────────────────╯${RESET}\n"
  printf "\n"
}

info() {
  printf "  ${BLUE}%s${RESET} %s\n" ">" "$*"
}

success() {
  printf "  ${GREEN}%s${RESET} %s\n" "+" "$*"
}

warn() {
  printf "  ${YELLOW}%s${RESET} %s\n" "!" "$*" >&2
}

step() {
  printf "\n  ${GOLD}%s${RESET} ${BOLD}%s${RESET}\n" ">>>" "$*"
}

done_msg() {
  printf "\n"
  printf "${GREEN}  ╭──────────────────────────────────────────╮${RESET}\n"
  printf "${GREEN}  │${RESET}${BOLD}   Agent is running. Happy building!      ${RESET}${GREEN}│${RESET}\n"
  printf "${GREEN}  ╰──────────────────────────────────────────╯${RESET}\n"
  printf "\n"
}

# ── First-run .env setup ─────────────────────────────────────────
if [[ ! -f ".env" && -f ".env.example" ]]; then
  cp .env.example .env
  banner
  printf "  ${GREEN}+${RESET} Created ${BOLD}.env${RESET} from .env.example\n"
  printf "\n"
  printf "  Before continuing, edit ${BOLD}.env${RESET} and set:\n"
  printf "\n"
  printf "    ${BOLD}AOC_BASE_URL${RESET}            ${DIM}https://api.ephapsys.com${RESET}\n"
  printf "    ${BOLD}AOC_ORG_ID${RESET}              ${DIM}from AOC > Organization${RESET}\n"
  printf "    ${BOLD}AOC_PROVISIONING_TOKEN${RESET}  ${DIM}from AOC > Organization > Tokens (boot_...)${RESET}\n"
  printf "    ${BOLD}AOC_MODULATION_TOKEN${RESET}    ${DIM}from AOC > Organization > Tokens (mod_...)${RESET}\n"
  printf "    ${BOLD}HF_TOKEN${RESET}                ${DIM}only if your model repo is private/gated${RESET}\n"
  printf "\n"
  printf "  ${DIM}Sign up at ${RESET}${BLUE}https://ephapsys.com${RESET}${DIM} if you don't have an account yet.${RESET}\n"
  printf "\n"
  printf "  Then rerun:\n"
  printf "    ${BOLD}./quickstart.sh${RESET}\n"
  printf "\n"
  exit 0
fi

banner

# ── Parse args ───────────────────────────────────────────────────
MODE="local"
ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gcp)   MODE="gcp"; shift ;;
    --local) MODE="local"; shift ;;
    *)       ARGS+=("$1"); shift ;;
  esac
done

info "Mode: ${BOLD}${MODE}${RESET}"

# ── Helpers ──────────────────────────────────────────────────────
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
    success "Templates already configured in .env"
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

# ── Resolve or bootstrap ─────────────────────────────────────────
step "Checking for existing templates"

if ! resolve_existing_templates; then
  step "Bootstrapping (first-time setup)"
  info "This registers models, runs modulation, and creates your agent template."
  info "It may take a few minutes on first run."
  printf "\n"
  if [[ "$MODE" == "gcp" ]]; then
    ./push.sh --mode gcp "${ARGS[@]+"${ARGS[@]}"}"
  else
    ./push.sh --mode local "${ARGS[@]+"${ARGS[@]}"}"
  fi
fi

# ── Launch ────────────────────────────────────────────────────────
step "Launching agent"

if [[ "$MODE" == "gcp" ]]; then
  ./run.sh --gcp
else
  ./run.sh --local
fi

done_msg
