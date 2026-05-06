# Scene 04 — operator-driven isolation via the AOC console.
# We pause the demo, ask the user to disable Agent B in the browser,
# and resume to show that:
#   - the cluster broadcasts the status change to remaining members (C sees it)
#   - subsequent sends to B are rejected
#   - re-enabling restores normal flow

run_scene_isolation() {
  local A_DID="$1"; local B_DID="$2"; local C_DID="$3"
  scene_header "04" "Operator isolation — disable B from the AOC console"
  narrate "When an agent is compromised or revoked, the platform must isolate"
  narrate "it from the mesh ${BOLD}immediately${RESET}. The status change broadcasts via"
  narrate "the cluster (issue #86), peers update their allowlists, and the"
  narrate "isolated agent can't send or receive A2A messages until re-enabled."
  narrate ""
  narrate "We'll trigger this from the AOC console (the same UI an operator"
  narrate "would use during a real incident)."
  narrate ""
  local console_url; console_url="$(aoc_console_url)"
  narrate "  ${BOLD}1.${RESET} Open: ${BLUE}${console_url}/agents${RESET}"
  narrate "  ${BOLD}2.${RESET} Find ${BOLD}helloworld-b${RESET} (or its public_id) and click ${BOLD}Disable${RESET}"
  narrate "  ${BOLD}3.${RESET} Come back here when done"
  narrate ""
  narrate "While B is disabled, watch ${BOLD}C's pane${RESET}: the cluster's status broadcast"
  narrate "should arrive there (visible as a [poll] summary or status_event)."
  wait_for_enter "Press Enter once B is disabled in the AOC"

  narrate ""
  narrate "Now A retries the same /ask we sent in scene 02:"
  tmux_send "$PANE_A" "/ask ${B_DID} Are you still there?"

  narrate ""
  narrate "Look at ${BOLD}A's pane${RESET}: the send is rejected by the platform"
  narrate "(B is no longer a valid recipient — the platform-side allowlist"
  narrate "blocks it before any inbox write)."
  narrate ""
  narrate "Note: today this enforcement is one-sided (issue #102) — sends ${BOLD}from${RESET}"
  narrate "B are blocked, but sends ${BOLD}to${RESET} B may currently still land in B's inbox."
  narrate "Either way, B's poller is paused while disabled, so nothing gets served."
  wait_for_enter
  narrate ""
  narrate "Re-enable B before continuing:"
  narrate "  ${BOLD}1.${RESET} Back in ${BLUE}${console_url}/agents${RESET}"
  narrate "  ${BOLD}2.${RESET} Click ${BOLD}Enable${RESET} on helloworld-b"
  wait_for_enter "Press Enter once B is re-enabled"
}
