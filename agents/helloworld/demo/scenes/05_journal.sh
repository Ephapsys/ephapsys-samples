# Scene 05 — message journal: per-agent audit trail.
# Each a2a_peer.py instance writes a tamper-evident JSONL journal of every
# message outcome (verified, rejected, guardrail_blocked, status_event).
# We cat all three journals so the developer sees the full picture.

run_scene_journal() {
  local A_DID="$1"; local B_DID="$2"; local C_DID="$3"
  local agents_dir="$AGENTS_DIR"

  scene_header "05" "The audit trail — per-agent message journal"
  narrate "Every outcome from the previous scenes is in a local journal."
  narrate "Each a2a_peer.py writes ${BOLD}a2a_journal.jsonl${RESET} in its own state dir."
  narrate "One line per message, one of: verified | rejected | guardrail_blocked"
  narrate "| status_event."
  narrate ""
  narrate "This is what an auditor or post-incident investigator reads to"
  narrate "answer 'who sent what to whom, and what did the recipient do with it?'"
  wait_for_enter "Press Enter to display the journals"

  for letter in a b c; do
    local jpath="${agents_dir}/helloworld-${letter}/a2a_journal.jsonl"
    printf "\n  ${GOLD}── helloworld-${letter} ──${RESET}  ${DIM}${jpath}${RESET}\n"
    if [[ -f "$jpath" ]]; then
      # Pretty-print each line with a leading prefix so multi-agent output
      # is scannable. Show the last 20 lines so a long-running demo doesn't
      # flood the pane.
      tail -n 20 "$jpath" | sed "s|^|    [${letter}] |"
    else
      printf "    ${DIM}(no journal yet — this agent hasn't processed any inbound messages)${RESET}\n"
    fi
  done

  narrate ""
  narrate "The full files (not just the tail above) live in each agent's"
  narrate "state dir. They survive across restarts and are append-only."
  wait_for_enter
}
