# Scene 05 — the audit journal.
# Framing: compliance auditor asks "who did what, when, with what outcome".
# Each a2a_peer.py instance writes a tamper-evident JSONL journal. We cat
# the last 20 lines from each so the developer sees the full picture.

run_scene_journal() {
  local A_DID="$1"; local B_DID="$2"; local C_DID="$3"
  local agents_dir="$AGENTS_DIR"

  scene_header "05" "The audit trail — per-agent message journal"
  narrate "Imagine a compliance auditor asks: ${BOLD}'In the last 5 minutes, who"
  narrate "said what to whom, and what did each recipient do with it?'${RESET}"
  narrate ""
  narrate "Every outcome from scenes 01-04 landed in a local journal."
  narrate "Each a2a_peer.py writes ${BOLD}a2a_journal.jsonl${RESET} in its own state dir,"
  narrate "one line per message: ${DIM}verified | rejected | guardrail_blocked"
  narrate "| status_event${RESET}."
  narrate ""
  narrate "Append-only, locally stored, recoverable across restarts. This is"
  narrate "the file the auditor reads — and it's also what an investigator"
  narrate "uses post-incident to trace adversarial influence."
  wait_for_enter "Press Enter to display the journals"

  for letter in a b c; do
    local jpath="${agents_dir}/helloworld-${letter}/a2a_journal.jsonl"
    printf "\n  ${GOLD}── helloworld-${letter} ──${RESET}  ${DIM}${jpath}${RESET}\n"
    if [[ -f "$jpath" ]]; then
      tail -n 20 "$jpath" | sed "s|^|    [${letter}] |"
    else
      printf "    ${DIM}(no journal yet — this agent hasn't processed any inbound messages)${RESET}\n"
    fi
  done

  you_saw "End-to-end audit story for one A2A flow" \
    "${BOLD}helloworld-a${RESET}: ${DIM}verified${RESET} entries — A acknowledging tool_results from B." \
    "${BOLD}helloworld-b${RESET}: ${DIM}verified${RESET} entries (legit messages) ${BOLD}plus${RESET} ${DIM}guardrail_blocked${RESET} (scene 03)." \
    "${BOLD}helloworld-c${RESET}: a ${DIM}status_event${RESET} (B's quarantine, scene 04)." \
    "" \
    "The full files (not just the tail above) live in each agent's" \
    "${BOLD}.ephapsys_state/a2a_journal.jsonl${RESET}. Append-only, survives restarts."
  wait_for_enter
}
