# Scene 02 — prompt serving: A asks B to run language.respond.
# Agent B is in real-inference mode (A2A_USE_TRUSTED_AGENT=1), so it
# actually loads the model and runs generation. Expect ~15s on staging
# (first call slightly longer due to lazy model load).

run_scene_prompt_serving() {
  local A_DID="$1"; local B_DID="$2"; local C_DID="$3"
  scene_header "02" "Prompt serving — A asks B to run language.respond"
  narrate "Agents can call each other's tools. Today B exposes one tool:"
  narrate "  ${BOLD}language.respond${RESET} — runs B's model on the supplied text."
  narrate ""
  narrate "Wire shape:"
  narrate "  request:  message_type=\"tool_call\", payload={tool, args}"
  narrate "  reply:    message_type=\"tool_result\", payload={ok, result}"
  narrate ""
  narrate "We'll have ${BOLD}A /ask B${RESET} for a one-line answer. B is in real-"
  narrate "inference mode, so this takes ~15s and lights up the GPU."
  wait_for_enter "Press Enter to ask"

  tmux_send "$PANE_A" "/ask ${B_DID} What is 2 plus 2? Reply with one short sentence."

  narrate ""
  narrate "Watch the flow:"
  narrate "  ${BOLD}A's pane${RESET} prints  ${DIM}'[-> ...] tool_call ... awaiting tool_result'${RESET}"
  narrate "  ${BOLD}B's pane${RESET} prints  ${DIM}'[loading TrustedAgent locally for inference...]'${RESET} (first call)"
  narrate "  ${BOLD}B's pane${RESET} prints  ${DIM}'[handled tool_call ... language.respond → ok]'${RESET}"
  narrate "  ${BOLD}A's pane${RESET} prints  ${DIM}'[<- ...] tool_result (took N.Ns): ...'${RESET}"
  narrate ""
  narrate "If you want to confirm B is actually running the model, open another"
  narrate "terminal and run ${BOLD}nvidia-smi -l 1${RESET} — you'll see the process appear."
  wait_for_enter
}
