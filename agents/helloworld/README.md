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

## Required Credentials

Populate `.env` with:

| Variable | Source | Required for |
|----------|--------|-------------|
| `AOC_BASE_URL` | `https://api.ephapsys.com` | All |
| `AOC_ORG_ID` | AOC > Organization | All |
| `AOC_PROVISIONING_TOKEN` | AOC > Organization > Tokens (`boot_...`) | Runtime |
| `AOC_MODULATION_TOKEN` | AOC > Organization > Tokens (`mod_...`) | Bootstrap (`push.sh`) |
| `HF_TOKEN` | Hugging Face | Only if model repo is private/gated |

Leave `MODEL_TEMPLATE_ID` and `AGENT_TEMPLATE_ID` blank on first run — `quickstart.sh` populates them.

## Common Commands

```bash
./quickstart.sh              # bootstrap + run (local)
./quickstart.sh --gcp        # bootstrap + run (GCP brain)
./quickstart.sh --fresh      # clear templates, re-bootstrap from scratch
./run.sh --local             # run locally (templates must exist)
./run.sh --gcp               # run on GCP (templates must exist)
./push.sh                    # bootstrap templates only
./push.sh --no-idempotent    # full modulation instead of idempotent publish
```

## Common Failures

| Error | Cause |
|-------|-------|
| `invalid provisioning token` | `AOC_PROVISIONING_TOKEN` is stale or wrong |
| `404 Agent template not found` | `AGENT_TEMPLATE_ID` is wrong or from a different environment |
| `language_model_not_ready` | Model exists but not fully published/modulated |
| `ZONE_RESOURCE_POOL_EXHAUSTED` | GPU not available in that zone (GCP) |

## Files

| File | Purpose |
|------|---------|
| `helloworld_agent.py` | Minimal TrustedAgent demo |
| `quickstart.sh` | Main entrypoint — bootstrap + run |
| `run.sh` | Runtime entrypoint |
| `push.sh` | Template bootstrap |
| `run_local.sh` | Local runtime helper |
| `run_gcp.sh` | GCP deployer with VM reuse |
