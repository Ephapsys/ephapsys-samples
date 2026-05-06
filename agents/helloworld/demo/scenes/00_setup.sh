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
    printf "\n  This demo orchestrates three trusted agents. Each one needs\n"
    printf "  its own personalized state dir. From ${BOLD}agents/${RESET}, run:\n\n"
    for letter in a b c; do
      local dir="$agents_dir/helloworld-$letter"
      if [[ ! -f "$dir/.ephapsys_state/agent_id" ]]; then
        printf "    ${DIM}# personalize agent ${letter}${RESET}\n"
        printf "    ${BOLD}cp -r helloworld helloworld-${letter}${RESET}\n"
        printf "    ${BOLD}cd helloworld-${letter} && ./quickstart.sh && cd ..${RESET}\n\n"
      fi
    done
    printf "  Each ${BOLD}quickstart.sh${RESET} run takes a few minutes (model download +\n"
    printf "  modulation), so plan accordingly. Re-run ${BOLD}./demo/run.sh${RESET} once\n"
    printf "  the three state dirs exist.\n\n"
    return 1
  fi

  success "All three agent state dirs present"
  return 0
}
