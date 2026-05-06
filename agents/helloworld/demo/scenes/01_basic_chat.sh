# Scene 01 — basic A2A peer-to-peer with cert verification.
# Driver tells Agent A to send a direct message to Agent B.
# Watch Agent B's pane: verify_message succeeds, payload is decoded.
# Watch Agent C's pane: stays silent (proves direct send is direct).

run_scene_basic_chat() {
  local A_DID="$1"; local B_DID="$2"; local C_DID="$3"
  scene_header "01" "Basic A→B chat with cert verification"
  narrate "Every A2A message carries the sender's X.509-signed identity."
  narrate "The receiver verifies that signature before processing the payload."
  narrate "We'll have ${BOLD}A direct-send to B${RESET} and watch ${BOLD}B's pane${RESET} verify it."
  wait_for_enter "Press Enter to send"

  tmux_send "$PANE_A" "@${B_DID} hello from agent A — can you hear me?"

  narrate "Look at ${BOLD}B's pane${RESET} (top-right): you should see"
  narrate "  ${DIM}\"[<- agent_inst_...] {'text': 'hello from agent A — can you hear me?'}\"${RESET}"
  narrate "Then look at ${BOLD}C's pane${RESET} (bottom-left): nothing arrives —"
  narrate "  C is in the cluster but the send was direct, not broadcast."
  wait_for_enter
}
