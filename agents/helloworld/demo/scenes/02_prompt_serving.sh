# Scene 02 — prompt serving in 5G ops context.
# Edge sees a critical error, asks cloud (bigger model) for analysis.
# B has A2A_USE_TRUSTED_AGENT=1 so it actually runs the model. ~15s.

run_scene_prompt_serving() {
  local A_DID="$1"; local B_DID="$2"; local C_DID="$3"
  scene_header "02" "Edge escalates a critical AMF error to cloud for analysis"
  narrate "The edge LLM is small (qwen3.5:0.8b — fast, runs on a Pi)."
  narrate "For deep root-cause analysis on a critical error, edge ${BOLD}escalates${RESET}"
  narrate "to the cloud agent, which has a bigger model and more context."
  narrate ""
  narrate "Wire shape:"
  narrate "  request:  ${DIM}message_type=\"tool_call\", payload={tool, args}${RESET}"
  narrate "  reply:    ${DIM}message_type=\"tool_result\", payload={ok, result}${RESET}"
  narrate ""
  narrate "Edge has just seen an AMF crash. We'll have ${BOLD}A /ask B${RESET} for"
  narrate "root-cause analysis. B is in real-inference mode — ~15s and"
  narrate "real GPU work."
  wait_for_enter "Press Enter to ask"

  tmux_send "$PANE_A" "/ask ${B_DID} An AMF service crashed with 'Invalid N1 message: malformed PDU at offset 0x32'. What's the likely root cause and what should the operator do?"
  sleep 3

  you_saw "Real prompt-serving over A2A — same shape Graham edge↔cloud will use" \
    "${BOLD}A's pane${RESET}:  ${DIM}'[-> …] tool_call language.respond cid=ask-… — awaiting tool_result'${RESET}" \
    "${BOLD}B's pane${RESET}:  ${DIM}'[loading TrustedAgent locally for inference…]'${RESET} (first call only)" \
    "${BOLD}B's pane${RESET}:  ${DIM}'[handled tool_call from … language.respond → ok]'${RESET} after ~15s" \
    "${BOLD}A's pane${RESET}:  ${DIM}'[<- …] tool_result (took N.Ns): …'${RESET} — actual LLM analysis text" \
    "" \
    "The wire protocol (${BOLD}tool_call/tool_result + correlation_id${RESET}) is generic." \
    "Graham's edge→cloud production feature uses exactly this shape." \
    "Bigger model lives on cloud, edge stays light."
  wait_for_enter
}
