# A2A Trust Demo

A guided walkthrough of agent-to-agent communication on the Ephapsys
platform — three trusted agents, five scenes, ~10–15 minutes.

The demo is set in a 5G operations context: agent **A** is an edge ops
console on a cell site, **B** is a cloud analyst with a bigger model in
the operator's NOC, **C** is a fleet observer. The wire protocol you see
here (`tool_call` → `tool_result` over A2A) is the same shape Graham
edge↔cloud uses in production.

## What this shows

| # | Scene | What you'll see |
|---|---|---|
| 01 | Edge sends a status update | A direct-sends to B; B verifies the X.509 signature on every hop. C stays silent. |
| 02 | Edge escalates to cloud | A `/ask`s B for root-cause analysis on a fake AMF crash; B runs the real model. |
| 03 | Adversarial input blocked | A sends prompt-injection text as if from a compromised log forwarder; B's guardrail blocks it. |
| 04 | Operator quarantine | You disable B in the AOC console; cluster broadcast propagates; A's retry is rejected. |
| 05 | Audit journal | Each agent's tamper-evident message journal is printed side by side. |

## One-command setup

From `agents/helloworld/`, after editing `.env` with AOC credentials:

```
./quickstart.sh --demo
```

This is the fastest path from a fresh checkout. It will:

1. **Bootstrap templates if needed** (`push.sh`) — registers a model + an
   agent template in the AOC. Skipped if `MODEL_TEMPLATE_ID` and
   `AGENT_TEMPLATE_ID` are already set in your `.env`.
2. **Provision three peer instances** (`demo/setup.sh`) — copies the
   template into `helloworld-a/`, `helloworld-b/`, `helloworld-c/`,
   creates each peer's venv, runs the personalize step (cert exchange +
   instance DID).
3. **Pre-warm the model** on `helloworld-b` only — B is the real-inference
   agent; A and C run in stub mode and skip the model.
4. **Launch the demo** (`./demo/run.sh`) — opens a tmux session and walks
   you through the five scenes.

First-time runtime: ~10–15 min (most of it is the model download in
step 1). Subsequent runs: a few seconds (everything's cached).

To re-bootstrap from scratch (clears templates *and* peer state dirs):

```
./quickstart.sh --demo --fresh
```

### Manual fallback

If you'd rather provision peers separately (e.g., to run on different
machines), use `./demo/setup.sh` directly after running plain
`./quickstart.sh` once:

```
./quickstart.sh                      # bootstrap templates only
./demo/setup.sh --peers 3            # provision a, b, c
./demo/run.sh                        # launch demo
```

`setup.sh` accepts `--peers N` (default 3, max 7), `--warmup <letter>`
(which peer pre-loads the model, default `b`), and `--fresh`.

## Prerequisites

- `tmux` for the recommended four-pane layout. Without it, the demo falls
  back to a `--no-tmux` mode that prints copy-paste commands for three
  terminals you open yourself.
- A GPU on this machine — scene 02 runs real inference (~15 s/call).
- Browser access to the AOC console for scene 04 (link is printed live).

## Running the demo

If you used `--demo` above, the demo launches automatically. If you've
already provisioned peers and just want to relaunch:

```
./demo/run.sh
```

This opens a tmux session named `a2a-demo` with four panes:

```
┌──────────────────────┬──────────────────────┐
│  Agent A             │  Agent B             │
│  edge ops console    │  cloud analyst       │
│  (stub mode)         │  (real inference)    │
├──────────────────────┼──────────────────────┤
│  Agent C             │  Driver              │
│  fleet observer      │  walks you through   │
│  (stub mode)         │  the five scenes     │
└──────────────────────┴──────────────────────┘
```

Focus the **Driver pane** (bottom-right). It narrates each scene, sends
commands to the agent panes via `tmux send-keys`, then prints a
"You just saw …" callback so you know what the relevant lines in the
agent panes mean. Press Enter to advance.

Detach anytime with `Ctrl-b d`. Reattach with `tmux attach -t a2a-demo`.

### Without tmux

```
./demo/run.sh --no-tmux
```

Prints the three `a2a_peer.py` commands to paste into three terminals you
open yourself. Driver narration becomes inline text telling you which
terminal to type into for each scene.

## How to read the panes during the demo

- **Agent A pane (edge console)** — issues sends. Outbound lines:
  `[-> ...] tool_call ...` and direct sends. Inbound replies:
  `[<- ...] tool_result (took N.Ns): ...`.
- **Agent B pane (cloud analyst)** — receives. Look for `[<- ...]`
  delivery lines, `[handled tool_call ...]` after running a tool, and
  guardrail-block entries when injection text is rejected.
- **Agent C pane (fleet observer)** — quiet during direct sends. Becomes
  vocal in scene 04 when the cluster broadcasts B's quarantine.
- **Driver pane** — your guide. Reads narration, runs each scene's
  `tmux_send` action, prints `You just saw …` callbacks.

The `[poll]` summary lines in agent panes are the SDK's regular inbox
poll results: `processed`, `verified`, `rejected`, `guardrail_blocked`,
`status_events`. These are exactly what got written to that agent's
journal in the last poll cycle.

## Configuration

The demo respects whatever `.env` values your three personalized dirs
have. Two demo-specific overrides:

| Var | Default | Purpose |
|---|---|---|
| `AOC_CONSOLE_URL` | derived from `AOC_BASE_URL` (strips `api.` prefix) | Where to send users in scene 04 to disable B |
| `A2A_USE_TRUSTED_AGENT` | `1` for B, unset for A & C | Toggles real inference vs. stub on the receiver |

## Troubleshooting

**`Missing personalized agent state for: ...`** — Run
`./quickstart.sh --demo` (or `./demo/setup.sh` if templates already exist).

**Agent B's pane never prints `[handled tool_call ...]`** — Confirm the
launch line includes `A2A_USE_TRUSTED_AGENT=1`. The demo sets it
automatically; only matters if you're running `--no-tmux` and copy-paste
without the prefix. Without it, B returns the echo stub.

**Scene 02 takes longer than 30 s** — Not expected if `--demo` ran the
warmup step. If you skipped warmup, the first `/ask` includes a one-time
lazy model load (~5–15 s). Subsequent calls are faster.

**Scene 04 says "send rejected" but B's pane still shows the message
arriving** — Today the platform's status check is one-way (issue #102 in
ephapsys-platform). Sends *from* a disabled agent are blocked, sends *to*
it may still land in B's inbox. B's poller is paused either way, so the
model never serves.

**Tmux pane sizes look squashed** — Default session size is 240 × 60
columns; small terminals get shrunk. Resize your terminal before running,
or use `--no-tmux` and lay the windows out yourself.

## Files

| Path | Purpose |
|---|---|
| `demo/run.sh` | Demo entry point; tmux orchestration + driver loop. |
| `demo/setup.sh` | Multi-peer provisioner (called by `quickstart.sh --demo`). |
| `demo/personalize_peer.py` | Per-peer verify + personalize (no chat loop). |
| `demo/lib.sh` | Shared helpers: colors, `tmux_send`, `wait_for_enter`, `you_saw`, mode runners. |
| `demo/scenes/00_setup.sh` | Preflight check (peers provisioned). |
| `demo/scenes/01_basic_chat.sh` | Edge → cloud direct send + cert verify. |
| `demo/scenes/02_prompt_serving.sh` | Edge `/ask` cloud for AMF root-cause. |
| `demo/scenes/03_guardrail.sh` | Adversarial input blocked at SDK boundary. |
| `demo/scenes/04_isolation.sh` | Operator quarantine via AOC console. |
| `demo/scenes/05_journal.sh` | Per-agent audit journal. |

## What's next

- The full A2A test harness lives at `agents/a2a_test_harness.py` —
  automated regression tests for the same wire protocol.
- The interactive peer (`a2a_peer.py`) accepts more commands than the
  demo uses: `/list`, `@<ref> <text>`, broadcast (no prefix). Try them
  while the demo is paused at any scene.
- Graham edge↔cloud is the production version of this flow: see
  `ephapsys-agents/Graham/docs/2026-05-06-edge-cloud-a2a-escalation.md`.
- Cluster-management UX, federation, and A2A transport architecture are
  tracked in ephapsys-platform issues #86, #87, #102, #104.
