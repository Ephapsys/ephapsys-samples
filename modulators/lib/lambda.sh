#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
#  lib/lambda.sh — shared Lambda Cloud helpers
# ════════════════════════════════════════════════════════════════
#
# Sourced by:
#   modulators/modulate_lambda_common.sh   (training, ephemeral VM)
#   agents/helloworld/run_lambda.sh        (runtime, persistent VM)
#
# Provides:
#   lambda_api               — REST wrapper around Lambda Cloud API
#   lambda_launch_instance   — capacity-aware launch with fallback list
#   lambda_wait_active       — poll until instance reaches active state
#   lambda_wait_ssh          — wait for SSH to become reachable
#   lambda_terminate         — terminate instance by id
#
# Caller-provided env (must be set before invoking the functions):
#   LAMBDA_API_KEY           — secret_api_...
#   LAMBDA_SSH_KEY_NAME      — registered SSH key name in Lambda dashboard
#   LAMBDA_API_BASE          — optional override; defaults to public endpoint
#
# Side-effect globals (caller may read after launch/wait):
#   LAMBDA_INSTANCE_ID       — set by lambda_launch_instance
#   LAMBDA_LAUNCHED_TYPE     — set by lambda_launch_instance
#   LAMBDA_LAUNCHED_REGION   — set by lambda_launch_instance
#   LAMBDA_HOST              — set by lambda_wait_active (instance public IP)

# Color codes — only set if unset so we don't stomp a caller's palette.
: "${BLUE:=\033[36m}"
: "${GREEN:=\033[32m}"
: "${YELLOW:=\033[33m}"
: "${RED:=\033[91m}"
: "${RESET:=\033[0m}"

LAMBDA_API_BASE_DEFAULT="https://cloud.lambdalabs.com/api/v1"

# Outputs populated by helpers below.
LAMBDA_INSTANCE_ID=""
LAMBDA_LAUNCHED_TYPE=""
LAMBDA_LAUNCHED_REGION=""
LAMBDA_HOST=""

# ── REST wrapper ───────────────────────────────────────────────
lambda_api() {
  local method="$1" path="$2"; shift 2
  curl -sS -u "${LAMBDA_API_KEY}:" -X "$method" \
    "${LAMBDA_API_BASE:-$LAMBDA_API_BASE_DEFAULT}${path}" \
    -H 'Content-Type: application/json' "$@"
}

# ── Capacity-aware launch ──────────────────────────────────────
# Args:
#   $1 — comma-separated instance type fallback list
#   $2 — name of the env var the caller used (for the failure hint;
#        defaults to LAMBDA_INSTANCE_TYPES if omitted)
# Side effects: sets LAMBDA_INSTANCE_ID / LAMBDA_LAUNCHED_TYPE / LAMBDA_LAUNCHED_REGION
# Returns 0 on success, 1 if no capacity in any type/region. On failure
# also prints a hint listing every instance type that currently has
# capacity, sorted by price, so the user knows what to add to the list.
lambda_launch_instance() {
  local types_csv="$1"
  local env_var_name="${2:-LAMBDA_INSTANCE_TYPES}"
  echo "🛰  Searching Lambda capacity across instance types..."
  local avail
  avail="$(lambda_api GET /instance-types 2>/dev/null || true)"
  if [ -z "$avail" ]; then
    printf "${RED}[ERROR]${RESET} Failed to query Lambda /instance-types — check LAMBDA_API_KEY\n" >&2
    return 1
  fi

  IFS=',' read -ra TYPES <<< "$types_csv"
  local i=0
  for itype in "${TYPES[@]}"; do
    i=$((i+1))
    local regions
    regions="$(echo "$avail" | jq -r ".data[\"$itype\"].regions_with_capacity_available[]?.name" 2>/dev/null || true)"
    if [ -z "$regions" ]; then
      printf "  [%d/%d] %-22s ${RED}✗ no capacity in any region${RESET}\n" "$i" "${#TYPES[@]}" "$itype"
      continue
    fi
    for region in $regions; do
      local body resp
      body="$(jq -n --arg t "$itype" --arg r "$region" --arg n "$LAMBDA_SSH_KEY_NAME" \
        '{region_name: $r, instance_type_name: $t, ssh_key_names: [$n], quantity: 1}')"
      resp="$(lambda_api POST /instance-operations/launch -d "$body" 2>/dev/null || true)"
      LAMBDA_INSTANCE_ID="$(echo "$resp" | jq -r '.data.instance_ids[0]? // empty' 2>/dev/null || echo "")"
      if [ -n "$LAMBDA_INSTANCE_ID" ]; then
        LAMBDA_LAUNCHED_TYPE="$itype"
        LAMBDA_LAUNCHED_REGION="$region"
        printf "  [%d/%d] %-22s ${GREEN}✓ launched in %s: %s${RESET}\n" \
          "$i" "${#TYPES[@]}" "$itype" "$region" "$LAMBDA_INSTANCE_ID"
        return 0
      fi
    done
  done

  # All requested types exhausted — show what IS available right now,
  # sorted by price, so the user can pick something to add to their env.
  lambda_print_available_capacity "$avail" "$env_var_name" >&2
  return 1
}

# Helper: pretty-print currently-available instance types from a cached
# /instance-types response. $1 = response JSON, $2 = env var name to
# mention in the "add one of these to X" hint.
lambda_print_available_capacity() {
  local avail="$1"
  local env_var_name="${2:-LAMBDA_INSTANCE_TYPES}"
  echo
  printf "${YELLOW}[hint]${RESET} Instance types currently available on Lambda (cheapest first):\n"
  local hint
  hint="$(echo "$avail" | jq -r '
    .data
    | to_entries[]
    | select(.value.regions_with_capacity_available | length > 0)
    | [
        (.value.instance_type.price_cents_per_hour // 999999),
        .key,
        (.value.regions_with_capacity_available | map(.name) | join(", "))
      ]
    | @tsv
  ' 2>/dev/null | sort -n)"
  if [ -z "$hint" ]; then
    printf "  ${RED}(none — all Lambda instance types are currently exhausted globally)${RESET}\n"
    return
  fi
  echo "$hint" | awk -F'\t' '{
    cents = $1
    if (cents == 999999) {
      printf "  %-22s  $?.??/hr  regions: %s\n", $2, $3
    } else {
      printf "  %-22s  $%.2f/hr  regions: %s\n", $2, cents/100.0, $3
    }
  }'
  echo
  printf "  To use, add one of these to %s in .env.lambda (or export inline before rerunning).\n" \
    "$env_var_name"
}

# ── Wait for instance active ───────────────────────────────────
# Args: $1 — instance id, $2 — max iterations (default 60, 5s each)
# Side effects: sets LAMBDA_HOST to the instance public IP.
# Returns 0 on active+IP, 1 on timeout or unhealthy/terminated state.
lambda_wait_active() {
  local instance_id="$1" max_iters="${2:-60}"
  echo "⏳ Waiting for instance to become active..."
  LAMBDA_HOST=""
  for i in $(seq 1 "$max_iters"); do
    local details status
    details="$(lambda_api GET "/instances/$instance_id" 2>/dev/null || echo '{}')"
    status="$(echo "$details" | jq -r '.data.status // "unknown"')"
    LAMBDA_HOST="$(echo "$details" | jq -r '.data.ip // empty')"
    printf "\r  ⏳ %d/%d (%-12s)..." "$i" "$max_iters" "$status"
    if [ "$status" = "active" ] && [ -n "$LAMBDA_HOST" ]; then
      printf "\n"
      return 0
    fi
    if [ "$status" = "unhealthy" ] || [ "$status" = "terminated" ]; then
      printf "\n"
      printf "${RED}[ERROR]${RESET} Instance entered %s state during boot\n" "$status" >&2
      return 1
    fi
    sleep 5
  done
  printf "\n"
  return 1
}

# ── Wait for SSH reachable ─────────────────────────────────────
# Args: $1 — host, $2 — ssh key path, $3 — max iterations (default 30, 10s each)
# Returns 0 if SSH responds within budget, 1 otherwise.
lambda_wait_ssh() {
  local host="$1" key_path="$2" max_iters="${3:-30}"
  local ssh_opts=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$key_path")
  echo "⏳ Waiting for SSH to become reachable..."
  for i in $(seq 1 "$max_iters"); do
    if ssh "${ssh_opts[@]}" -o ConnectTimeout=5 "ubuntu@${host}" 'echo ok' >/dev/null 2>&1; then
      printf "  ${GREEN}✓ SSH ready (~%ds)${RESET}\n" $((i * 10))
      return 0
    fi
    sleep 10
  done
  return 1
}

# ── Terminate instance ─────────────────────────────────────────
# Args: $1 — instance id (no-op if empty). Best-effort; never raises.
lambda_terminate() {
  local instance_id="$1"
  [ -n "$instance_id" ] || return 0
  local body
  body="$(jq -n --arg id "$instance_id" '{instance_ids: [$id]}')"
  lambda_api POST /instance-operations/terminate -d "$body" >/dev/null 2>&1 || true
}
