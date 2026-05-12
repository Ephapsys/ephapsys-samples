# Scene 00 — preflight: verify the three demo agent dirs are provisioned.
# Sourced by run.sh BEFORE any tmux session is created. Exits non-zero
# if anything is missing so run.sh can bail with a clean error.

run_scene_setup() {
  local agents_dir="$1"
  local missing=()

  for letter in a b c; do
    local dir="$agents_dir/helloworld-$letter"
    if [[ ! -f "$dir/.ephapsys_state/agent_id" ]]; then
      missing+=("helloworld-$letter (no agent_id)")
    elif [[ ! -x "$dir/.venv/bin/python" ]]; then
      missing+=("helloworld-$letter (no .venv)")
    fi
  done

  if (( ${#missing[@]} > 0 )); then
    err "Missing personalized agent state for: ${missing[*]}"
    printf "\n  Provision the demo agents with one command from ${BOLD}helloworld/${RESET}:\n\n"
    printf "    ${BOLD}./quickstart.sh --demo${RESET}\n\n"
    printf "  This bootstraps templates (if needed), creates the three peer\n"
    printf "  state dirs, personalizes each instance, pre-warms the model on\n"
    printf "  helloworld-b, then launches the demo. ~10-15 min on first run,\n"
    printf "  seconds on subsequent runs.\n\n"
    printf "  ${DIM}If you already ran quickstart but want to redo just provisioning,\n"
    printf "  call ${BOLD}./demo/setup.sh${RESET}${DIM} directly.${RESET}\n\n"
    return 1
  fi

  # Peers need AOC_A2A_TOKEN for the /agents DID-resolution lookup. setup.sh
  # syncs each peer's .env from helloworld/.env, so a missing token here
  # almost always means it's commented out in the source.
  local peer_env="$agents_dir/helloworld-a/.env"
  if [[ -f "$peer_env" ]] && ! grep -qE '^[[:space:]]*AOC_A2A_TOKEN=.+' "$peer_env"; then
    err "AOC_A2A_TOKEN is not set in $peer_env"
    printf "\n  a2a_peer.py needs an A2A-scoped token to look up peer DIDs via\n"
    printf "  the AOC ${BOLD}/agents${RESET} endpoint. Without it, every agent pane will\n"
    printf "  exit with a 401 before the demo can start.\n\n"
    printf "  ${BOLD}Fix:${RESET}\n"
    printf "    1. Uncomment ${BOLD}AOC_A2A_TOKEN=${RESET} in ${BOLD}helloworld/.env${RESET}\n"
    printf "       (or generate a new token in the AOC console under tokens).\n"
    printf "    2. Re-sync to peer dirs: ${BOLD}./demo/setup.sh${RESET}\n\n"
    return 1
  fi

  success "All three agent state dirs present"

  # Verify each peer is ENABLED in the AOC. Scene 04 explicitly disables B
  # and asks the user to re-enable; if they skip the re-enable and rerun,
  # the demo would otherwise hit "send rejected" mid-scene-01 with no
  # obvious cause. Hit /agents once and check all three by DID.
  if ! command -v jq >/dev/null 2>&1; then
    warn "jq not installed — skipping AOC agent-status preflight"
    return 0
  fi
  local aoc_base aoc_token
  aoc_base="$(grep -E '^[[:space:]]*AOC_BASE_URL=' "$peer_env" | head -1 | sed -E 's/^[[:space:]]*AOC_BASE_URL=//;s/^["'"'"']//;s/["'"'"']$//')"
  aoc_token="$(grep -E '^[[:space:]]*AOC_A2A_TOKEN=' "$peer_env" | head -1 | sed -E 's/^[[:space:]]*AOC_A2A_TOKEN=//;s/^["'"'"']//;s/["'"'"']$//')"

  local agents_json
  agents_json="$(curl -fsS --max-time 10 "$aoc_base/agents" \
    -H "Authorization: Bearer $aoc_token" 2>/dev/null || true)"
  if [[ -z "$agents_json" ]]; then
    warn "Could not fetch $aoc_base/agents (network or token issue) — skipping enabled-status check"
    return 0
  fi

  local not_enabled=() not_found=()
  for letter in a b c; do
    local did status
    did="$(cat "$agents_dir/helloworld-$letter/.ephapsys_state/agent_id")"
    status="$(printf '%s' "$agents_json" | jq -r --arg did "$did" \
      'first(.[] | select(.did == $did) | .status) // "NOT_FOUND"')"
    case "$status" in
      ENABLED) ;;
      NOT_FOUND) not_found+=("helloworld-$letter") ;;
      *) not_enabled+=("helloworld-$letter (status=$status)") ;;
    esac
  done

  if (( ${#not_found[@]} > 0 || ${#not_enabled[@]} > 0 )); then
    err "AOC agent-status preflight failed:"
    if (( ${#not_found[@]} > 0 )); then
      printf "    ${RED}•${RESET} not visible in /agents: ${BOLD}%s${RESET}\n" "${not_found[*]}"
      printf "      (re-personalize: ${BOLD}./demo/setup.sh --fresh${RESET})\n"
    fi
    if (( ${#not_enabled[@]} > 0 )); then
      printf "    ${RED}•${RESET} not enabled: ${BOLD}%s${RESET}\n" "${not_enabled[*]}"
      printf "      Re-enable in the AOC console:\n"
      printf "        ${BLUE}%s/agents${RESET}\n" "$(aoc_console_url)"
    fi
    printf "\n"
    return 1
  fi
  success "All three agents are ENABLED in the AOC"

  # Drain any backlog in each peer's inbox before the demo launches. The
  # SDK poller in a2a_peer.py delivers every unacked message on its first
  # poll — without this, leftover messages from prior demo runs (old
  # chat, old /ask tool_results, old system.status_change broadcasts from
  # scene 04) land in the agent panes the moment the demo starts and
  # look like the agents are talking on their own. Same drain pattern
  # the harness uses in its phase-2 bootstrap (a2a_test_harness.py).
  local drained_total=0 drain_errors=0
  for letter in a b c; do
    local did inbox_json msg_ids count
    did="$(cat "$agents_dir/helloworld-$letter/.ephapsys_state/agent_id")"
    inbox_json="$(curl -fsS --max-time 10 \
      "$aoc_base/a2a/messages/inbox?agent_id=$did&limit=200&include_acked=false" \
      -H "Authorization: Bearer $aoc_token" 2>/dev/null || true)"
    if [[ -z "$inbox_json" ]]; then
      warn "helloworld-$letter: could not fetch inbox — skipping drain"
      drain_errors=$(( drain_errors + 1 ))
      continue
    fi
    msg_ids="$(printf '%s' "$inbox_json" | jq -r '.items[]?.id // empty')"
    count=0
    while IFS= read -r mid; do
      [[ -z "$mid" ]] && continue
      if curl -fsS --max-time 10 -X POST \
          "$aoc_base/a2a/messages/$mid/ack" \
          -H "Authorization: Bearer $aoc_token" \
          -H "Content-Type: application/json" \
          --data "$(jq -nc --arg did "$did" '{agent_id:$did}')" \
          >/dev/null 2>&1; then
        count=$(( count + 1 ))
      else
        drain_errors=$(( drain_errors + 1 ))
      fi
    done <<< "$msg_ids"
    drained_total=$(( drained_total + count ))
    if (( count > 0 )); then
      printf "    ${DIM}drained helloworld-%s: %d message(s)${RESET}\n" "$letter" "$count"
    fi
  done

  if (( drain_errors > 0 )); then
    warn "Inbox drain had $drain_errors ack error(s) — demo may still show stale messages"
  fi
  if (( drained_total > 0 )); then
    success "Drained $drained_total backlog message(s) from peer inboxes"
  else
    success "All peer inboxes already empty"
  fi
  return 0
}
