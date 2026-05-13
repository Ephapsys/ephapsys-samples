# Scene 01 — basic A2A peer-to-peer with cert verification.
# Set in a 5G ops context: A is an edge ops console, B is a cloud analyst,
# C is a fleet observer that stays silent during direct sends.

run_scene_basic_chat() {
  local A_DID="$1"; local B_DID="$2"; local C_DID="$3"
  scene_header "01" "Edge sends a status update — verified at the cloud"
  narrate "Imagine A is an ${BOLD}edge ops console${RESET} on a 5G cell site,"
  narrate "B is a ${BOLD}cloud analyst${RESET} agent in the operator's NOC,"
  narrate "and C is a ${BOLD}fleet observer${RESET} that watches the cluster."
  narrate ""
  narrate "Every A2A message carries the sender's X.509 signature."
  narrate "The receiver verifies that signature before processing the payload."
  narrate "We'll have ${BOLD}A direct-send${RESET} a status update to ${BOLD}B${RESET}."
  wait_for_enter "Press Enter to send"

  tmux_send "$PANE_A" "@${B_DID} status-check from edge-1: AMF healthy, 142 active sessions"
  sleep 2

  you_saw "Cert-verified peer-to-peer messaging" \
    "${BOLD}B's pane${RESET} (top-right): a ${DIM}'[<- agent_inst_…] {text: …}'${RESET} line means" \
    "  B's ${BOLD}process_inbox${RESET} polled, ${BOLD}A2AClient.verify_message${RESET} validated A's" \
    "  X.509 cert chain, and the payload was decoded." \
    "${BOLD}C's pane${RESET} (bottom-left): silent — direct sends don't fan out." \
    "If A's cert had been forged, B would log ${DIM}[rejected: bad_signature]${RESET} instead."
  wait_for_enter
}
