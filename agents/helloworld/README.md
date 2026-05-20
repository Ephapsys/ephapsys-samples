# HelloWorld Agent

The smallest useful Ephapsys agent. It can:

- Personalize an agent instance against the AOC
- Prepare the runtime automatically
- Open an interactive chat session locally, on GCP, or on Lambda Cloud

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
chatbot). The two phases use **different GPUs by default** — A100-first
for training, A10-first for inference (with H100 fallbacks) — so cost
matches the workload.

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

#### Two GPU fallback lists

Modulation and runtime are tuned independently — set both in `.env.lambda`:

| Variable                           | Default                                                            | Used by                |
| ---------------------------------- | ------------------------------------------------------------------ | ---------------------- |
| `LAMBDA_INSTANCE_TYPES`            | `gpu_1x_a100, gpu_1x_a100_sxm4, gpu_1x_a10`                        | `push.sh --lambda` (modulation) |
| `LAMBDA_RUNTIME_INSTANCE_TYPES`    | `gpu_1x_a10, gpu_1x_a100, gpu_1x_a100_sxm4, gpu_1x_h100_pcie, gpu_1x_h100_sxm5, gpu_2x_h100_sxm5` | `run.sh --lambda` (runtime)     |

The launcher tries each type in order until one has capacity, so
cheap-first ordering keeps the typical bill low. H100s on the runtime
list are last-resort fallbacks when A10/A100 are exhausted.

#### Two usage patterns

| Pattern              | Commands                                     | When to use                                                |
| -------------------- | -------------------------------------------- | ---------------------------------------------------------- |
| **Modulate-only**    | `./push.sh --lambda` then `./run.sh --local` | Cheapest. ~30 min of A100 time, then runtime is free locally. |
| **Full Lambda**      | `./quickstart.sh --lambda`                   | Training + persistent agent VM on Lambda. Bills hourly.    |

#### Capacity safeguards built into `--lambda`

- **Runtime preflight** — before kicking off ~30 min of modulation,
  `quickstart.sh` probes Lambda to confirm at least one runtime instance
  type has capacity. If everything is exhausted, it aborts *before* any
  spend and prints what GPUs are currently available.
- **Capacity hint on launch failure** — when no configured type has
  capacity, the launcher prints every currently-available type (with
  prices and regions), sorted cheapest-first, so you know exactly what
  to add to `LAMBDA_RUNTIME_INSTANCE_TYPES` or `LAMBDA_INSTANCE_TYPES`.
- **Named instances** — VMs launched by these scripts show up in your
  Lambda dashboard as `ephapsys-helloworld-runtime-<timestamp>` and
  `ephapsys-language-modulate-<timestamp>` so they're easy to find.

#### Cost reference (live Lambda pricing as of 2026-05)

| Instance             | $/hr   | Where it appears                |
| -------------------- | ------ | ------------------------------- |
| `gpu_1x_a10`         | $1.29  | Runtime default (1st choice)    |
| `gpu_1x_a100`        | $1.99  | Modulation default (1st), runtime fallback |
| `gpu_1x_a100_sxm4`   | $1.99  | Modulation + runtime fallback   |
| `gpu_1x_h100_pcie`   | $3.29  | Runtime fallback (H100 PCIe)    |
| `gpu_1x_h100_sxm5`   | $4.29  | Runtime fallback (H100 SXM5)    |
| `gpu_2x_h100_sxm5`   | $8.38  | Runtime fallback (last resort)  |

Confirm current prices at <https://lambdalabs.com/service/gpu-cloud>.

#### ⚠ Terminate the runtime VM when finished

The runtime VM stays up after `run_lambda.sh` exits — the agent is
designed to outlive the script, and on failure the VM is left up so
you can SSH in and debug. The script prints a termination command in
its footer; the dashboard at <https://cloud.lambdalabs.com/instances>
also works.

#### Recovering from a failed run / BYO instance

If `run_lambda.sh` provisioned a VM but failed later (timeout, SSH
hiccup, apt error), or if you've manually launched a VM via Lambda's
dashboard, reuse the existing instance instead of launching a new one:

```bash
./run.sh --lambda --attach <instance_id>
# or via env:
LAMBDA_ATTACH_INSTANCE=<instance_id> ./run.sh --lambda
```

With `--attach`, the script:

- Skips capacity probing and provisioning
- Looks up the existing instance (errors out if terminated/unhealthy)
- Waits for it to become active if still booting
- Runs setup idempotently (`apt`, venv, pip — safe to re-run)
- Kills any prior tmux session of the same name and starts a fresh one
- Suppresses the termination command in the footer (terminating a VM
  you pre-launched would be surprising)

If `run_lambda.sh` errors out after the VM is up, the error message
prints both the SSH-to-debug command and the `--attach` resume command
with the instance ID already filled in.

#### Known limitations

- No managed ingress on Lambda — agent gets a raw public IP. BYO
  firewall / DNS / TLS if you need to expose it.
- Capacity is not guaranteed — the launcher falls back through the
  configured types but if all are exhausted, the script exits and you
  retry later. The preflight + capacity-hint reduce wasted time but
  cannot reserve capacity (Lambda doesn't support reservations).
- Capacity may also be lost between preflight and runtime launch
  (the ~30 min modulation window) — use `--attach` to resume if so.
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
./quickstart.sh                          # bootstrap + run (local)
./quickstart.sh --gcp                    # bootstrap + run (GCP brain)
./quickstart.sh --lambda                 # bootstrap + run (Lambda Cloud, persistent VM)
./quickstart.sh --fresh                  # clear templates, re-bootstrap from scratch
./run.sh --local                         # run locally (templates must exist)
./run.sh --gcp                           # run on GCP (templates must exist)
./run.sh --lambda                        # run on Lambda Cloud (templates must exist; bills hourly)
./run.sh --lambda --attach <instance>    # resume on an existing Lambda VM (recovery / BYO)
./push.sh                                # bootstrap templates only
./push.sh --lambda                       # bootstrap + modulate on Lambda Cloud
./push.sh --no-idempotent                # full modulation instead of idempotent publish
```

## Common Failures

| Error                                                 | Cause                                                                |
| ----------------------------------------------------- | -------------------------------------------------------------------- |
| `invalid provisioning token`                          | `AOC_PROVISIONING_TOKEN` is stale or wrong                           |
| `404 Agent template not found`                        | `AGENT_TEMPLATE_ID` is wrong or from a different environment         |
| `language_model_not_ready`                            | Model exists but not fully published/modulated                       |
| `ZONE_RESOURCE_POOL_EXHAUSTED`                        | GPU not available in that zone (GCP)                                 |
| `No runtime capacity available across LAMBDA_RUNTIME_INSTANCE_TYPES` | Lambda preflight failed — widen the list or wait for capacity |
| `all types/regions exhausted` (Lambda)                | Modulation or runtime list exhausted; see the printed capacity hint  |
| `Instance never became active` (Lambda)               | Boot exceeded 12 min — resume with `./run.sh --lambda --attach <id>` |

## Files

| File                                    | Purpose                                                                                |
| --------------------------------------- | -------------------------------------------------------------------------------------- |
| `helloworld_agent.py`                   | Minimal TrustedAgent demo                                                              |
| `quickstart.sh`                         | Main entrypoint — bootstrap + run (with Lambda runtime preflight)                      |
| `run.sh`                                | Runtime entrypoint (dispatches to `run_local.sh` / `run_gcp.sh` / `run_lambda.sh`)     |
| `push.sh`                               | Template bootstrap + modulation dispatch                                               |
| `run_local.sh`                          | Local runtime helper                                                                   |
| `run_gcp.sh`                            | GCP deployer with VM reuse                                                             |
| `run_lambda.sh`                         | Lambda Cloud runtime deployer (persistent VM, billed hourly; supports `--attach`)      |
| `.env.lambda.example`                   | Template for Lambda credentials + GPU fallback lists                                   |
| `../../modulators/lib/lambda.sh`        | Shared Lambda Cloud helpers (sourced by `run_lambda.sh` and `modulate_lambda_common.sh`) |
