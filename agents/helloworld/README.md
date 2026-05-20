# HelloWorld Agent

The smallest useful Ephapsys agent. It can:

- Personalize an agent instance against the AOC
- Prepare the runtime automatically
- Open an interactive chat session locally or on GCP

## Quick Start

```bash
cd agents/helloworld
cp .env.example .env    # fill in AOC credentials
./quickstart.sh
```

`quickstart.sh` will:

- Create `.env` from `.env.example` if needed, then stop so you can fill in credentials
- Reuse existing templates when `MODEL_TEMPLATE_ID` / `AGENT_TEMPLATE_ID` are set
- Otherwise bootstrap starter templates via `./push.sh`
- Launch the agent via `./run.sh`

### Start fresh

To re-bootstrap from scratch (clears templates and agent state):

```bash
./quickstart.sh --fresh
```

### GCP mode

```bash
cp .env.gcp.example .env.gcp   # fill in GCP project settings
./quickstart.sh --gcp
```

### Lambda Cloud mode

Lambda is supported for **both** modulation (training) and runtime (the
chatbot). The two phases use different GPUs by default — A100 for
training, A10 for inference — so cost matches the workload.

```bash
cp .env.lambda.example .env.lambda   # fill in Lambda creds
./quickstart.sh --lambda
```

`.env.lambda` needs three values from your Lambda Cloud account:

| Variable              | Where to find it                                                |
| --------------------- | --------------------------------------------------------------- |
| `LAMBDA_API_KEY`      | <https://cloud.lambdalabs.com/api-keys> (one-time, ~1 min)      |
| `LAMBDA_SSH_KEY_NAME` | Lambda dashboard → SSH Keys → the registered key name           |
| `LAMBDA_SSH_KEY_PATH` | Local path to the matching `.pem` (run `chmod 400` on the file) |

#### Two usage patterns

| Pattern              | Commands                                  | When to use                                                |
| -------------------- | ----------------------------------------- | ---------------------------------------------------------- |
| **Modulate-only**    | `./push.sh --lambda` then `./run.sh --local` | Cheapest. ~30 min of A100 time, then runtime is free locally. |
| **Full Lambda**      | `./quickstart.sh --lambda`                | Training + persistent agent VM on Lambda. Bills hourly.    |

#### Cost reference (approximate, check Lambda for current pricing)

| Instance       | $/hr   | Used for       |
| -------------- | ------ | -------------- |
| `gpu_1x_a10`     | ~$0.75 | Runtime (default) |
| `gpu_1x_a100`    | ~$1.29 | Modulation (default) |
| `gpu_1x_a100_sxm4` | ~$1.99 | Modulation fallback |
| `gpu_1x_h100_pcie` | ~$2.49 | Modulation override |
| `gpu_2x_h100_sxm5` | ~$5.98 | Fastest modulation |

#### ⚠ Terminate the runtime VM when finished

`run_lambda.sh` defaults to `AUTO_DELETE=false`, so the agent VM stays
up after the script exits. The script prints a termination command in
its footer; the dashboard at <https://cloud.lambdalabs.com/instances>
also works.

#### Known limitations

- No managed ingress on Lambda — agent gets a raw public IP. BYO
  firewall / DNS / TLS if you need to expose it.
- Capacity is not guaranteed — the launcher falls back through the list
  in `LAMBDA_INSTANCE_TYPES` / `LAMBDA_RUNTIME_INSTANCE_TYPES`. If none
  have capacity, the script exits and you retry later.
- `--a2a-demo` is not supported with `--lambda`. The peer cluster needs
  persistent local peers; use `--a2a-demo` alone (local mode).

## Required Credentials

Populate `.env` with:

| Variable                 | Source                                   | Required for                        |
| ------------------------ | ---------------------------------------- | ----------------------------------- |
| `AOC_BASE_URL`           | `https://api.ephapsys.com`               | All                                 |
| `AOC_ORG_ID`             | AOC > Organization                       | All                                 |
| `AOC_PROVISIONING_TOKEN` | AOC > Organization > Tokens (`boot_...`) | Runtime                             |
| `AOC_MODULATION_TOKEN`   | AOC > Organization > Tokens (`mod_...`)  | Bootstrap (`push.sh`)               |
| `HF_TOKEN`               | Hugging Face                             | Only if model repo is private/gated |

Leave `MODEL_TEMPLATE_ID` and `AGENT_TEMPLATE_ID` blank on first run — `quickstart.sh` populates them.

## Want to see the full A2A trust story?

Run **one** command from this directory:

```bash
./quickstart.sh --a2a-demo
```

This bootstraps the templates (if needed), provisions three peer agents
(`helloworld-a/-b/-c`), pre-warms the model on B, and launches a
four-scene guided walkthrough — basic chat, prompt serving with real
inference, adversarial input blocked, operator quarantine via the AOC
console. ~10–15 min on a fresh checkout, seconds
on subsequent runs. See [demo/README.md](demo/README.md) for what each
scene shows and how to read the four-pane tmux layout.

## Common Commands

```bash
./quickstart.sh              # bootstrap + run (local)
./quickstart.sh --gcp        # bootstrap + run (GCP brain)
./quickstart.sh --lambda     # bootstrap + run (Lambda Cloud, persistent VM)
./quickstart.sh --fresh      # clear templates, re-bootstrap from scratch
./run.sh --local             # run locally (templates must exist)
./run.sh --gcp               # run on GCP (templates must exist)
./run.sh --lambda            # run on Lambda Cloud (templates must exist; bills hourly)
./push.sh                    # bootstrap templates only
./push.sh --lambda           # bootstrap + modulate on Lambda Cloud
./push.sh --no-idempotent    # full modulation instead of idempotent publish
```

## Common Failures

| Error                          | Cause                                                        |
| ------------------------------ | ------------------------------------------------------------ |
| `invalid provisioning token`   | `AOC_PROVISIONING_TOKEN` is stale or wrong                   |
| `404 Agent template not found` | `AGENT_TEMPLATE_ID` is wrong or from a different environment |
| `language_model_not_ready`     | Model exists but not fully published/modulated               |
| `ZONE_RESOURCE_POOL_EXHAUSTED` | GPU not available in that zone (GCP)                         |

## Files

| File                  | Purpose                           |
| --------------------- | --------------------------------- |
| `helloworld_agent.py` | Minimal TrustedAgent demo         |
| `quickstart.sh`       | Main entrypoint — bootstrap + run |
| `run.sh`              | Runtime entrypoint                |
| `push.sh`             | Template bootstrap                |
| `run_local.sh`        | Local runtime helper              |
| `run_gcp.sh`          | GCP deployer with VM reuse        |
| `run_lambda.sh`       | Lambda Cloud runtime deployer (persistent VM, billed hourly) |
