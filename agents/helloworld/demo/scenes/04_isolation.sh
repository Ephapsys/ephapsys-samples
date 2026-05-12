# Scene 04 — operator quarantine via the AOC console.
# Framing: operator suspects the cloud analyst is compromised, takes it
# offline. Cluster broadcast propagates; A's retry is rejected.

run_scene_isolation() {
  local A_DID="$1"; local B_DID="$2"; local C_DID="$3"
  scene_header "04" "Operator quarantine — cluster broadcast propagates within seconds"
  narrate "Suppose the SOC suspects the cloud analyst (B) has been compromised."
  narrate "They quarantine it from the AOC console. Cluster status broadcast"
  narrate "(issue #86) propagates to every peer; allowlists update in seconds;"
  narrate "the isolated agent can't send or receive A2A messages until re-enabled."
  narrate ""
  local console_url; console_url="$(aoc_console_url)"
  narrate "  ${BOLD}1.${RESET} Open: ${BLUE}${console_url}/agents${RESET}"
  narrate "  ${BOLD}2.${RESET} Find the helloworld-b agent (its public_id is shown in B's pane banner)"
  narrate "     and click ${BOLD}Disable${RESET}"
  narrate "  ${BOLD}3.${RESET} Come back here when done"
  narrate ""
  narrate "Watch ${BOLD}C's pane${RESET} (bottom-left, the fleet observer): you should see"
  narrate "the cluster's status_change message arrive once you flip the switch."
  wait_for_enter "Press Enter once B is disabled in the AOC"

  narrate ""
  narrate "Now A retries the same /ask we sent in scene 02:"
  tmux_send "$PANE_A" "/ask ${B_DID} Are you still there?"
  sleep 3

  you_saw "Send rejected at the platform boundary — B is no longer reachable" \
    "${BOLD}A's pane${RESET}: no ${DIM}tool_result${RESET} arrives; the platform rejected the send." \
    "${BOLD}C's pane${RESET}: a ${DIM}status_event${RESET} entry appeared (cluster broadcast)." \
    "${BOLD}B's pane${RESET}: poller still running but the platform paused inbox delivery." \
    "" \
    "Today this is one-sided (issue #102 — sends ${BOLD}from${RESET} a disabled agent are blocked," \
    "sends ${BOLD}to${RESET} it may still queue in the inbox). Either way, B's poller is paused" \
    "and nothing gets served." \
    "" \
    "Re-enable B before continuing scene 05:" \
    "  ${BOLD}1.${RESET} Back in ${BLUE}${console_url}/agents${RESET}" \
    "  ${BOLD}2.${RESET} Click ${BOLD}Enable${RESET} on helloworld-b"
  wait_for_enter "Press Enter once B is re-enabled"
}
