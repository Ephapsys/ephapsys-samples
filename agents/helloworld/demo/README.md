# A2A Trust Demo

A guided walkthrough of agent-to-agent communication on the Ephapsys
platform — three trusted agents, five scenes, ~15 minutes.

## What this shows

Five scenes building from "two agents talking" to the full operational
trust story:

| # | Scene | What you'll see |
|---|---|---|
| 01 | Basic chat | A direct-sends to B; B verifies the X.509 signature on every hop. |
| 02 | Prompt serving | A asks B to run `language.respond`; B runs a real model and returns the result. |
| 03 | Guardrail block | A sends a prompt-injection payload; the platform blocks it at B's SDK boundary, before the model sees it. |
| 04 | Operator isolation | You disable B in the AOC console; the cluster broadcasts the status change, A's retry is rejected. |
| 05 | Audit journal | Each agent's tamper-evident message journal is printed side by side. |

## Prerequisites

You need three personalized agent state dirs alongside this `helloworld/`
template — `helloworld-a/`, `helloworld-b/`, `helloworld-c/`. Each gets
its own X.509 cert, agent identity, and modulated model.

If they don't exist yet, from `agents/`:

```
cp -r helloworld helloworld-a
cd helloworld-a && ./quickstart.sh && cd ..
cp -r helloworld helloworld-b
cd helloworld-b && ./quickstart.sh && cd ..
cp -r helloworld helloworld-c
cd helloworld-c && ./quickstart.sh && cd ..
```

Each `quickstart.sh` takes a few minutes (model download + modulation).
`./demo/run.sh` will tell you exactly what's missing if you skip a step.

You also need:
- `tmux` (for the recommended layout) — fall back to `--no-tmux` if you don't.
- A GPU on this machine — scene 02 runs real inference (~15 s/call).
- Browser access to the AOC console for scene 04 (link is printed live).

## Running the demo

From `helloworld/`:

```
./demo/run.sh
```

This opens a tmux session named `a2a-demo` with four panes:

```
┌──────────────────────┬──────────────────────┐
│  Agent A             │  Agent B             │
│  (a2a_peer.py)       │  (a2a_peer.py,       │
│   stub mode          │   real inference)    │
├──────────────────────┼──────────────────────┤
│  Agent C             │  Driver              │
│  (a2a_peer.py)       │  (the script that    │
│   stub mode          │   walks you through) │
└──────────────────────┴──────────────────────┘
```

Focus the **Driver pane** (bottom-right). It narrates each scene, sends
commands to the agent panes via `tmux send-keys`, and waits for you to
press Enter between scenes. You watch the agent panes to see the effect
of each command.

Detach anytime with `Ctrl-b d`. Reattach with `tmux attach -t a2a-demo`.

### Without tmux

```
./demo/run.sh --no-tmux
```

This prints the three `python a2a_peer.py` commands to paste into three
terminals you open yourself. The Driver narration becomes inline text
that tells you which terminal to type into for each scene.

## How to read the panes during the demo

- **Agent A pane** — issues sends. Look here for `[-> ...] tool_call ...`
  outbound lines and `[<- ...] tool_result (took N.Ns): ...` inbound replies.
- **Agent B pane** — receives. Look here for `[<- ...]` direct messages,
  `[handled tool_call ...]` after running a tool, and guardrail-block
  entries when injection text is rejected.
- **Agent C pane** — observer. Should stay quiet during direct sends
  (proving the message routing was direct, not broadcast). Becomes vocal
  during scene 04 when the cluster broadcasts B's status change.
- **Driver pane** — your guide. Reads narration, advance scenes with Enter.

The `[poll]` summary lines in agent panes are the SDK's regular inbox-poll
results: `processed`, `verified`, `rejected`, `guardrail_blocked`,
`status_events`. These are what got written to that agent's journal in
the last poll cycle.

## Configuration

The demo respects whatever `.env` values your three personalized dirs have.
Two demo-specific overrides:

| Var | Default | Purpose |
|---|---|---|
| `AOC_CONSOLE_URL` | derived from `AOC_BASE_URL` (strips `api.` prefix) | Where to send users in scene 04 to disable B |
| `A2A_USE_TRUSTED_AGENT` | `1` for B, unset for A & C | Toggles real inference vs. stub on the receiver |

## Troubleshooting

**`Missing personalized agent state for: ...`** — Run `quickstart.sh` in
the missing dir(s). See Prerequisites above.

**Agent B's pane never prints `[handled tool_call ...]`** — Confirm the
launch line includes `A2A_USE_TRUSTED_AGENT=1` (the demo sets it
automatically; only matters if you're running `--no-tmux` and copy-pasted
without the prefix). Without it, B returns the echo stub.

**Scene 02 takes longer than 30 s** — First call to `TrustedAgent.run()`
on B includes a one-time lazy model load. Subsequent `/ask` calls are
faster. You can warm B up before the demo by typing
`/ask <B-DID> hi` once in A's pane.

**Scene 04 says "send rejected" but B's pane still shows the message
arriving** — Today the platform's status check is one-way (issue #102 in
ephapsys-platform). Sends *from* a disabled agent are blocked, sends *to*
it may currently still land in its inbox. The poller is paused either
way, so the model never serves.

**Tmux pane sizes look squashed** — Default session size is `240 × 60`
columns; small terminals get shrunk. Resize your terminal before running,
or run with `--no-tmux` and lay out the windows yourself.

## What's next

- The full A2A test harness lives at `agents/a2a_test_harness.py` —
  automated regression tests for the same wire protocol.
- The interactive peer (`a2a_peer.py`) accepts more commands than the
  demo uses: `/list`, `@<ref> <text>`, broadcast (no prefix). Try them
  while the demo is paused at any scene.
- Cluster-management UX, federation, and A2A transport architecture are
  tracked in ephapsys-platform issues #86, #87, #102, #104.
