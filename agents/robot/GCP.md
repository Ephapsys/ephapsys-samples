# Robot GCP Setup

Robot GCP mode deploys only the brain remotely.
Your local machine still owns:
- microphone
- camera
- speaker
- terminal face

## Files

- [`.env`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/.env)
  - runtime credentials uploaded to the remote brain VM
- [`.env.gcp`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/.env.gcp)
  - local GCP deployment settings
- [`.env.gcp.example`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/.env.gcp.example)
  - tracked template
- [`.last_gcp_instance`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/.last_gcp_instance)
  - local metadata for the reusable robot brain VM

## Recommended Flow

Initial full bootstrap:

```bash
cd ephapsys-sdk/samples/agents/robot
cp .env.example .env
cp .env.gcp.example .env.gcp
./quickstart.sh --gcp
```

Fast repeat run once templates already exist and a brain VM has been created:

```bash
./run.sh --gcp --reuse-instance
```

That repeat path is the intended operator workflow when GPU quota is tight.

## Required `.env`

Fill in:
- `AOC_BASE_URL`
- `AOC_ORG_ID`
- `AOC_PROVISIONING_TOKEN`
- `AGENT_TEMPLATE_ID`

Optional for bootstrap work:
- `AOC_MODULATION_TOKEN`

Important:
- the robot brain fails hard if `AOC_PROVISIONING_TOKEN` is stale
- updating local `.env` is enough for the next reuse run, because `run_gcp.sh` uploads `.env` each time

## Required `.env.gcp`

Set:
- `PROJECT_ID`
- `ZONE`
- `MACHINE_TYPE`
- `DISK_SIZE`
- `IMAGE_FAMILY`
- `IMAGE_PROJECT`
- `INSTANCE_PREFIX`

Recommended GPU search settings:

```ini
USE_GPU=1
GPU_TYPE=nvidia-tesla-t4
GPU_MACHINE_TYPE=n1-standard-8
GPU_IMAGE_FAMILY=pytorch-2-7-cu128-ubuntu-2204-nvidia-570
GPU_FALLBACKS=t4,l4
ZONE_FALLBACKS=us-central1-a,us-central1-b,us-central1-c,us-central1-f
REGION_FALLBACKS=us-central1,us-east1,us-west1
```

## Behavior

`run_gcp.sh` now supports:
- `--reuse-instance`
- `--fresh-instance`

Default behavior:
- try to reuse the VM from `.last_gcp_instance`
- only provision a new brain VM if reuse is unavailable

This matters because many projects will only have `1` GPU available globally.
Fresh GPU brains now switch to a Deep Learning GPU image family automatically instead of the plain CPU Ubuntu image.

## Runtime Steps

On a fresh brain VM:
1. create/select a GPU VM
2. prepare minimal system packages remotely
3. upload robot brain files and `.env`
4. install/update remote Python deps
5. start the remote brain service
6. wait for remote port `8765`
7. open the SSH tunnel
8. wait for local forwarded port
9. launch the local robot face/body client

On a reused brain VM:
1. reuse existing VM metadata
2. refresh robot files and `.env`
3. reinstall/update remote deps as needed
4. restart the brain service
5. wait for readiness
6. reopen the tunnel and local client

## Useful Commands

Run normal GCP mode:

```bash
./run.sh --gcp
```

Force reuse explicitly:

```bash
./run.sh --gcp --reuse-instance
```

Force fresh reprovisioning:

```bash
./run.sh --gcp --fresh-instance
```

Preflight:

```bash
./check_gcp.sh
```

Stop the current brain VM to save cost while preserving state:

```bash
gcloud compute instances stop <instance> --project <project> --zone <zone>
```

Delete it only if you intentionally want to throw away the prepared runtime:

```bash
gcloud compute instances delete <instance> --project <project> --zone <zone>
```

## Notes

- `.last_gcp_instance` is local metadata only and is gitignored
- the launcher now writes `.last_gcp_instance` early, so reuse survives partial failures better
- `requirements_brain.txt` is the authoritative remote brain dependency set
- remote brain startup depends on a valid provisioning token in `.env`
- GPU quota and zonal stock are separate constraints
- the launcher now fails fast if `nvidia-smi` is missing or `torch.cuda.is_available()` is false on a GPU VM
