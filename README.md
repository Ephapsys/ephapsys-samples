This folder contains the following:

- **agents:**  sample code of various agents using Ephapsys SDK (TrustedAgent class)
- **modulators:**  sample code for modulating various models using Ephapsys SDK (ModulatorClient class)

## Install profiles for samples

Use the matching install command before running each sample:

| Sample type | Recommended install |
|---|---|
| `agents/helloworld` | `pip install ephapsys` |
| `modulators/*` (training/modulation only) | `pip install ephapsys` |
| `modulators/*` (full eval/report stack) | `pip install "ephapsys[all]"` |

For `agents/helloworld`, the local wrapper can bootstrap a fresh checkout for you:

```bash
cd agents/helloworld
./quickstart.sh
```

The script creates `.venv`, installs the local SDK with `modulation` extras if needed, resolves or creates the HelloWorld model/template assets, and runs backend preflight automatically before startup.

## A2A trust demo (multi-agent)

A guided four-scene walkthrough showing agent-to-agent communication on top of the Ephapsys platform — three trusted peers in a cluster, real model inference via `tool_call`, SDK-side guardrails, and operator-driven isolation propagating through the cluster.

From a fresh checkout:

```bash
cd agents/helloworld
./quickstart.sh --a2a-demo
```

One command bootstraps templates, provisions three peer instances (`helloworld-a/-b/-c`) with their own DIDs and certs, forms an A2A cluster from the three peers, pre-warms the model on B, and opens a four-pane tmux session.

| Scene | What it shows |
|---|---|
| 01 basic chat | A direct-sends to B; B verifies the X.509 identity and sender status before delivery. |
| 02 prompt-serving | A `/ask`s B to run `language.respond`; B runs real inference and replies with `tool_result`. |
| 03 guardrail | A sends a prompt-injection payload; the SDK's guardrail blocks it at B's receive boundary before the model sees it. |
| 04 operator isolation | You disable B in the AOC console; the cluster broadcasts a `status_change` to A and C, and A's retry to B is rejected. |

Other flags:

```bash
./quickstart.sh --a2a-demo --fresh           # wipe templates + peer dirs, re-bootstrap
./quickstart.sh --a2a-demo-peers 5           # provision five peers instead of three
./demo/setup.sh                              # re-run peer provisioning + cluster only
./demo/run.sh                                # relaunch the tmux session
```

Requires `tmux`, a CUDA-capable GPU on the local machine (scene 02 runs real inference), and AOC console access for scene 04. See [`agents/helloworld/demo/README.md`](agents/helloworld/demo/README.md) for the four-pane layout and how to read each pane during the demo.
