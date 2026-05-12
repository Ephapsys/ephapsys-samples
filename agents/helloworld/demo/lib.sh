# Shared helpers for the helloworld A2A demo.
# Sourced by run.sh and scene scripts.

# ── Colors (match helloworld/quickstart.sh) ─────────────────────
BOLD="\033[1m"
DIM="\033[2m"
BLUE="\033[36m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
GOLD="\033[38;5;220m"
WHITE="\033[97m"
RESET="\033[0m"

# ── Output helpers ──────────────────────────────────────────────
banner() {
  printf "\n${GOLD}"
  printf "    ╔═══════════════════════════════════════════════════╗\n"
  printf "    ║                                                   ║\n"
  printf "    ║   ${WHITE}${BOLD}⚡ Ephapsys A2A Demo — Full Trust Story ⚡${RESET}${GOLD}      ║\n"
  printf "    ║                                                   ║\n"
  printf "    ║   ${RESET}${DIM}Three agents, one trusted mesh, end-to-end${RESET}${GOLD}      ║\n"
  printf "    ║                                                   ║\n"
  printf "    ╚═══════════════════════════════════════════════════╝\n${RESET}\n"
}

scene_header() {
  local num="$1"; shift
  printf "\n  ${GOLD}━━━ Scene ${num} ━━━${RESET}  ${BOLD}%s${RESET}\n\n" "$*"
}

narrate() {
  printf "  ${BLUE}»${RESET} %b\n" "$*"
}

success() {
  printf "  ${GREEN}+${RESET} %b\n" "$*"
}

warn() {
  printf "  ${YELLOW}!${RESET} %b\n" "$*" >&2
}

err() {
  printf "  ${RED}✖${RESET} %b\n" "$*" >&2
}

separator() {
  printf "  ${DIM}%s${RESET}\n" "────────────────────────────────────────────────"
}

wait_for_enter() {
  local prompt="${1:-Press Enter to continue}"
  printf "\n  ${DIM}${prompt}…${RESET} "
  read -r _
}

# Close the loop after a tmux_send: tell the user what landed in the
# agent panes and why it matters. First arg is a short title; remaining
# args are bullet lines that describe what to look for.
you_saw() {
  local title="$1"; shift
  printf "\n  ${GREEN}━ You just saw ━${RESET}  ${BOLD}%s${RESET}\n" "$title"
  for line in "$@"; do
    printf "    ${DIM}•${RESET} %b\n" "$line"
  done
}

# ── tmux helpers ────────────────────────────────────────────────
# Pane indices set by run_tmux_mode after the splits:
#   PANE_A=0  top-left
#   PANE_B=1  top-right
#   PANE_C=2  bottom-left
#   PANE_DRIVER=3  bottom-right
PANE_A=0
PANE_B=1
PANE_C=2
PANE_DRIVER=3
TMUX_SESSION="a2a-demo"

# Mode-aware key sender. In tmux mode it sends the line via send-keys to
# the named pane. In manual mode it tells the user to type it themselves
# in the appropriate terminal and waits for Enter.
tmux_send() {
  local pane="$1"; shift
  local cmd="$*"
  if [[ "${DEMO_MODE:-tmux}" == "tmux" ]]; then
    tmux send-keys -t "${TMUX_SESSION}:0.${pane}" "$cmd" Enter
  else
    local label
    case "$pane" in
      "$PANE_A") label="Agent A's terminal" ;;
      "$PANE_B") label="Agent B's terminal" ;;
      "$PANE_C") label="Agent C's terminal" ;;
      *)          label="terminal #$pane" ;;
    esac
    printf "\n  ${YELLOW}>${RESET} In ${BOLD}%s${RESET}, type:\n" "$label"
    printf "    ${BOLD}%s${RESET}\n" "$cmd"
    printf "  ${DIM}then press Enter here once you've typed it.${RESET} "
    read -r _
  fi
}

# ── AOC console URL ─────────────────────────────────────────────
# AOC_BASE_URL points at the API host (e.g. api.staging.ephapsys.ai).
# The console host is the same with the "api." prefix stripped, falling
# back to the raw URL if there's no api. prefix. Override with AOC_CONSOLE_URL.
aoc_console_url() {
  if [[ -n "${AOC_CONSOLE_URL:-}" ]]; then
    printf '%s' "$AOC_CONSOLE_URL"
    return
  fi
  local base="${AOC_BASE_URL:-}"
  base="${base%/}"
  if [[ "$base" == https://api.* ]]; then
    printf '%s' "https://${base#https://api.}"
  else
    printf '%s' "$base"
  fi
}

# Read an agent's DID (instance) from its state dir.
agent_did() {
  local dir="$1"
  cat "$dir/.ephapsys_state/agent_id"
}

# Poll Agent B's tmux pane until a2a_peer.py prints the eager-load
# sentinel "[ready: model in GPU]". Times out after BREADY_TIMEOUT_S
# (default 180s — staging model download + load can take that long on a
# cold venv). Returns 0 if marker seen, 1 if timeout.
wait_for_b_ready() {
  local timeout_s="${BREADY_TIMEOUT_S:-180}"
  local interval=2
  local elapsed=0
  local snap
  printf "  ${BLUE}»${RESET} waiting for ${BOLD}helloworld-b${RESET} to load model into GPU"
  while (( elapsed < timeout_s )); do
    snap="$(tmux capture-pane -t "${TMUX_SESSION}:0.${PANE_B}" -p 2>/dev/null || true)"
    # Match "[ready: model loaded on ...]" regardless of device suffix.
    # The full line tells the user whether they got cuda:0 or fell back
    # to CPU; the marker alone is enough to advance the demo.
    if printf '%s' "$snap" | grep -qE '\[ready: model loaded on '; then
      local ready_line
      ready_line="$(printf '%s' "$snap" | grep -oE '\[ready: model loaded on [^]]+\]' | tail -1)"
      printf " ${GREEN}done${RESET} (${elapsed}s)\n"
      printf "    ${DIM}%s${RESET}\n" "$ready_line"
      if printf '%s' "$ready_line" | grep -q 'CPU'; then
        warn "Model loaded on CPU — scene 02 inference will be slow."
        warn "Check: nvidia-smi, torch CUDA install, CUDA_VISIBLE_DEVICES."
      fi
      return 0
    fi
    # Detect early death: a2a_peer.py prints a Traceback and exits if
    # something's wrong (bad token, missing peer DID, etc). Bail fast
    # with the captured tail so the user doesn't wait 3 minutes for
    # nothing.
    if printf '%s' "$snap" | grep -qE 'Traceback \(most recent call|raise SystemExit|requests\.exceptions'; then
      printf " ${RED}failed${RESET}\n"
      err "Agent B exited before model load. Tail of B's pane:"
      printf '%s\n' "$snap" | tail -12 | sed 's/^/    /'
      return 1
    fi
    printf "."
    sleep "$interval"
    elapsed=$(( elapsed + interval ))
  done
  printf " ${RED}timeout${RESET}\n"
  err "Agent B did not print '[ready: model in GPU]' within ${timeout_s}s."
  err "Inspect B's pane: ${BOLD}tmux attach -t ${TMUX_SESSION}${RESET} (then Ctrl-b q to find pane numbers)"
  return 1
}

# ── Mode runners ────────────────────────────────────────────────
# Run all five scenes against the current driver context. Both modes
# call this once the agent panes/terminals are running.
run_all_scenes() {
  run_scene_basic_chat        "$A_DID" "$B_DID" "$C_DID"
  run_scene_prompt_serving    "$A_DID" "$B_DID" "$C_DID"
  run_scene_guardrail         "$A_DID" "$B_DID" "$C_DID"
  run_scene_isolation         "$A_DID" "$B_DID" "$C_DID"
  run_scene_journal           "$A_DID" "$B_DID" "$C_DID"

  printf "\n  ${GREEN}━━━ demo complete ━━━${RESET}\n\n"
}

# Tear down the tmux session and free GPU memory held by Agent B's
# TrustedAgent. Called from the driver pane after run_all_scenes, and
# also via trap if the driver exits unexpectedly.
teardown_tmux() {
  tmux kill-session -t "${TMUX_SESSION}" 2>/dev/null || true
}

run_tmux_mode() {
  export DEMO_MODE="tmux"

  # Bail if we're already inside the demo session (re-entrant run).
  if [[ "${TMUX:-}" == *"a2a-demo"* ]]; then
    err "Already inside the a2a-demo tmux session. Detach first (Ctrl-b d)."
    exit 1
  fi

  # If a stale session exists, kill it.
  if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    warn "Killing stale tmux session ${TMUX_SESSION}"
    tmux kill-session -t "$TMUX_SESSION"
  fi

  separator
  narrate "Launching tmux session ${BOLD}${TMUX_SESSION}${RESET} with 4 panes."
  narrate "Detach anytime with ${BOLD}Ctrl-b d${RESET}; reattach with ${BOLD}tmux attach -t ${TMUX_SESSION}${RESET}."

  # Build the four-pane layout:
  #   pane 0 = top-left (A)        pane 1 = top-right (B)
  #   pane 2 = bottom-left (C)     pane 3 = bottom-right (Driver)
  tmux new-session -d -s "$TMUX_SESSION" -x 240 -y 60
  tmux split-window -h -t "${TMUX_SESSION}:0.0"
  tmux split-window -v -t "${TMUX_SESSION}:0.0"
  tmux split-window -v -t "${TMUX_SESSION}:0.1"
  tmux select-layout -t "${TMUX_SESSION}:0" tiled

  # Each pane gets A2A_PEER_AGENT_ID listing the other two DIDs — a2a_peer.py
  # exits at import if neither A2A_PEER_AGENT_ID nor A2A_CLUSTER_ID is set.
  # Pane 0 — Agent A (stub mode). Use the per-agent venv python so we
  # don't depend on a system-wide `python` symlink.
  tmux send-keys -t "${TMUX_SESSION}:0.${PANE_A}" \
    "cd $AGENTS_DIR/helloworld-a && A2A_PEER_AGENT_ID='$B_DID,$C_DID' .venv/bin/python a2a_peer.py" Enter

  # Pane 1 — Agent B (real inference)
  tmux send-keys -t "${TMUX_SESSION}:0.${PANE_B}" \
    "cd $AGENTS_DIR/helloworld-b && A2A_PEER_AGENT_ID='$A_DID,$C_DID' A2A_USE_TRUSTED_AGENT=1 .venv/bin/python a2a_peer.py" Enter

  # Pane 2 — Agent C (stub mode)
  tmux send-keys -t "${TMUX_SESSION}:0.${PANE_C}" \
    "cd $AGENTS_DIR/helloworld-c && A2A_PEER_AGENT_ID='$A_DID,$B_DID' .venv/bin/python a2a_peer.py" Enter

  # Pane 3 — Driver. Re-launch this script with a private flag so the
  # driver-loop body runs inside the pane (uses the same scenes + lib).
  tmux send-keys -t "${TMUX_SESSION}:0.${PANE_DRIVER}" \
    "cd $DEMO_DIR && bash run.sh --__driver-loop" Enter

  # Give the agent panes ~2s to print their banners before attach.
  sleep 2
  tmux attach -t "$TMUX_SESSION"
}

run_manual_mode() {
  export DEMO_MODE="manual"

  separator
  narrate "Manual orchestration. Open three terminals and run, in order:"
  printf "\n"
  printf "    ${BOLD}# Agent A (stub mode)${RESET}\n"
  printf "    ${BOLD}cd $AGENTS_DIR/helloworld-a && A2A_PEER_AGENT_ID='$B_DID,$C_DID' .venv/bin/python a2a_peer.py${RESET}\n\n"
  printf "    ${BOLD}# Agent B (real inference)${RESET}\n"
  printf "    ${BOLD}cd $AGENTS_DIR/helloworld-b && A2A_PEER_AGENT_ID='$A_DID,$C_DID' A2A_USE_TRUSTED_AGENT=1 .venv/bin/python a2a_peer.py${RESET}\n\n"
  printf "    ${BOLD}# Agent C (stub mode)${RESET}\n"
  printf "    ${BOLD}cd $AGENTS_DIR/helloworld-c && A2A_PEER_AGENT_ID='$A_DID,$B_DID' .venv/bin/python a2a_peer.py${RESET}\n\n"
  narrate "Wait until each one shows ${DIM}me = ...${RESET} and a ${DIM}> ${RESET} prompt."
  narrate "On Agent B specifically, wait for the ${BOLD}[ready: model in GPU]${RESET} line"
  narrate "(eager model load; takes 5-30s on a warm cache, longer on first run)."
  wait_for_enter "Press Enter when all three terminals are running AND B shows [ready: model in GPU]"
  run_all_scenes

  narrate "Demo done. Stop the agents to free GPU memory:"
  narrate "  In each of the three terminals, press ${BOLD}Ctrl-D${RESET} (or Ctrl-C)."
  narrate "Agent B's loaded model is what's holding the GPU; once you stop it,"
  narrate "${BOLD}nvidia-smi${RESET} should show the process gone."
}

# Driver loop — runs inside the bottom-right tmux pane via --__driver-loop.
run_driver_loop() {
  export DEMO_MODE="tmux"

  # Source scenes again (we're a child shell, scope is fresh).
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

  banner
  narrate "You are in the ${BOLD}driver pane${RESET}. The other three panes are running"
  narrate "agents A (top-left), B (top-right) and C (bottom-left)."
  narrate "Agents A and C run in stub mode (no model load); B is the real-inference"
  narrate "target and pre-loads its model into GPU before we start."
  printf "\n"
  if ! wait_for_b_ready; then
    err "Aborting demo. Fix B's pane and rerun ${BOLD}./demo/run.sh${RESET}."
    return 1
  fi
  wait_for_enter "Press Enter to begin Scene 01"

  # If the driver crashes mid-scene, still clean up the agents so the GPU
  # is freed. Disabled before normal completion (we want to give the user
  # a chance to inspect first) and re-engaged at the kill prompt.
  trap teardown_tmux EXIT

  run_all_scenes

  trap - EXIT  # disable EXIT-trap teardown — user controls timing now
  narrate "Re-run any scene by sourcing it: ${BOLD}source $SCENES_DIR/02_prompt_serving.sh${RESET}"
  narrate "Then call: ${BOLD}run_scene_prompt_serving \"\$A_DID\" \"\$B_DID\" \"\$C_DID\"${RESET}"
  narrate ""
  narrate "When you press Enter below, the tmux session will close and"
  narrate "Agent B's loaded model will be unloaded from the GPU."
  wait_for_enter "Press Enter to close the demo and free the GPU"
  teardown_tmux
}

