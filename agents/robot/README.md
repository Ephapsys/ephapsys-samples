# Robot Agent (Sample with Ephapsys SDK)

This sample is a trusted multimodal robot demo built on the Ephapsys SDK.
It can hear, see, think, and speak after verification and personalization.

The sample has three logical pieces:
- `body` handles microphone, camera, and speaker I/O
- `brain` owns runtime preparation, trusted verification, memory, and model orchestration
- `face` is the terminal UI developers interact with

There are two supported deployment shapes:
- local mode: `body + brain + face` all run on the same machine
- GCP mode: only the `brain` runs remotely; the local machine keeps the `body` and terminal `face`

## Fastest Paths

Local default:

```bash
cd ephapsys-sdk/samples/agents/robot
./quickstart.sh
```

GCP brain:

```bash
cd ephapsys-sdk/samples/agents/robot
./quickstart.sh --gcp
```

Important distinction:
- `./quickstart.sh --gcp` does full push/bootstrap first, then runs the remote-brain flow
- `./run.sh --gcp` is the faster path once templates already exist in AOC

## Required Credentials

Populate [`.env`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/.env) with at least:
- `AOC_BASE_URL`
- `AOC_ORG_ID`
- `AOC_PROVISIONING_TOKEN`
- `AGENT_TEMPLATE_ID`

Needed for template/bootstrap work:
- `AOC_MODULATION_TOKEN` or equivalent bootstrap credential used by `push.sh`

Important:
- `AOC_PROVISIONING_TOKEN` is a runtime credential. If it is stale or rotated, the remote brain will fail with:
  - `Provisioning token exchange failed (401): invalid provisioning token`
- `run_gcp.sh` uploads the current local `.env` to the remote VM each run, so updating local `.env` is enough for a reuse run.

## Local Workflow

Recommended first run:

```bash
cd ephapsys-sdk/samples/agents/robot
cp .env.example .env
./quickstart.sh
```

Direct entrypoints:

```bash
./push.sh
./run.sh
./run.sh --local
./run.sh --local smoke
```

For repo-local SDK development only:

```bash
ROBOT_USE_LOCAL_SDK=1 ./run.sh --local
```

## Bootstrap / Templates

`push.sh` handles the robot template/bootstrap path.

Examples:

```bash
./push.sh
./push.sh --gcp
./push.sh --no-idempotent
ROBOT_ENABLE_WORLD_MODEL=1 ./push.sh
```

What it does:
- registers or reuses the baseline robot model templates
- prefers idempotent publish by default
- can run full modulation when requested
- resolves or reuses the robot agent template
- writes `AGENT_TEMPLATE_ID` into local `.env`

Important operational note:
- once the agent template already exists in AOC, later push/modulation runs mainly update model state
- the `AGENT_TEMPLATE_ID` may stay the same while the underlying model/template artifacts change

## GCP Brain Workflow

The robot GCP path is brain-only remote deployment:
- remote: robot brain FastAPI service
- local: microphone, camera, speaker, and terminal face

That means the interactive terminal experience still happens locally. The difference is that model orchestration and inference live on the GCP VM.

### Recommended first GCP run

```bash
cd ephapsys-sdk/samples/agents/robot
cp .env.example .env
cp .env.gcp.example .env.gcp
./quickstart.sh --gcp
```

### Faster repeat runs

Once templates already exist and a brain VM has already been created, use:

```bash
./run.sh --gcp --reuse-instance
```

That path should:
- reuse the existing robot brain VM from [`.last_gcp_instance`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/.last_gcp_instance)
- upload the latest robot files and `.env`
- reinstall/update remote dependencies if needed
- restart the remote brain
- wait for the brain port and SSH tunnel to be ready
- launch the local face/body client

This is the correct workflow under tight GPU quotas.

### Force a fresh brain VM

```bash
./run.sh --gcp --fresh-instance
```

Only do this when you intentionally want to reprovision the brain VM.
With a quota like `GPUS_ALL_REGIONS=1`, blind reprovisioning is usually the wrong move.

### Useful GCP commands

Run the brain remotely while keeping body/face local:

```bash
./run.sh --gcp
```

Explicitly reuse the last brain VM:

```bash
./run.sh --gcp --reuse-instance
```

Force fresh provisioning instead of reuse:

```bash
./run.sh --gcp --fresh-instance
```

Preflight:

```bash
./check_gcp.sh
```

Stop the current brain VM to save cost while preserving its disk/runtime state:

```bash
gcloud compute instances stop <instance> --project <project> --zone <zone>
```

Delete it only when you want to discard the prepared runtime completely:

```bash
gcloud compute instances delete <instance> --project <project> --zone <zone>
```

## Recommended `.env.gcp` settings

Populate [`.env.gcp`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/.env.gcp) with at least:
- `PROJECT_ID`
- `ZONE`
- `MACHINE_TYPE`
- `DISK_SIZE`
- `IMAGE_FAMILY`
- `IMAGE_PROJECT`
- `INSTANCE_PREFIX`

Recommended for GPU search:
- `USE_GPU=1`
- `GPU_TYPE=nvidia-tesla-t4`
- `GPU_MACHINE_TYPE=n1-standard-8`
- `GPU_IMAGE_FAMILY=pytorch-2-7-cu128-ubuntu-2204-nvidia-570`
- `GPU_FALLBACKS=t4,l4`
- `ZONE_FALLBACKS=us-central1-a,us-central1-b,us-central1-c,us-central1-f`
- `REGION_FALLBACKS=us-central1,us-east1,us-west1`

Notes:
- `run_gcp.sh` clamps too-small boot disks up to `100GB`
- GPU VMs now switch to a Deep Learning image family automatically instead of reusing the plain CPU Ubuntu image
- the launcher now writes `.last_gcp_instance` early, so reuse survives partial failures better
- `.last_gcp_instance` is local metadata only and is gitignored

## Operational Reality

Robot GCP depends on two distinct concerns:

1. **Infrastructure availability**
- GPU quota
- zonal GPU stock
- correct zone/GPU combinations

2. **Runtime credential validity**
- the robot brain still needs a valid `AOC_PROVISIONING_TOKEN`
- if provisioning tokens are rotated, the remote brain will crash until local `.env` is updated and re-uploaded

This is why the current reliable operator loop is:
1. get one working brain VM
2. keep it running
3. update local `.env` when credentials change
4. reuse the VM with `./run.sh --gcp --reuse-instance`

## Common Failures

- `Provisioning token exchange failed (401): invalid provisioning token`
  - local `.env` contains a stale `AOC_PROVISIONING_TOKEN`
- `GPUS_ALL_REGIONS exceeded`
  - another GPU VM is already consuming your global quota
- `ZONE_RESOURCE_POOL_EXHAUSTED`
  - GPU exists in principle, but not in that zone right now
- `Machine type ... does not exist in zone`
  - the GPU family / machine type combination is not valid there
- `nvidia-smi: command not found` or `torch.cuda.is_available() == False`
  - the guest image is not GPU-ready; the launcher now fails fast on this condition
- local face connects to `127.0.0.1:<port>` and gets `ConnectionRefused`
  - historically this meant the remote brain was not ready yet; `run_gcp.sh` now waits for remote and local ports before launching the client
- `Turn failed: Transformers/Pillow not installed`
  - this message can also be a downstream symptom of a broken language runtime; check remote GPU readiness and `~/robot_brain.log`

## Permissions

Unlike a browser app, this Python demo does not pop OS permission dialogs for microphone/camera.
You need your operating system to allow your terminal/Python interpreter to access those devices.

- macOS
  - System Settings → Privacy & Security → Microphone / Camera
- Windows
  - Settings → Privacy → Microphone / Camera
- Linux
  - ensure your user can access relevant `/dev/video*` and audio devices

If permissions are wrong:
- mic capture may be silent
- camera capture may fail or return blank frames

## Files

- [`robot_agent.py`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/robot_agent.py) → thin launcher for local mode
- [`robot_channel.py`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/robot_channel.py) → local typed event/command boundary
- [`robot_body.py`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/robot_body.py) → microphone, camera, and speaker I/O
- [`robot_brain.py`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/robot_brain.py) → runtime orchestration and memory
- [`robot_brain_server.py`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/robot_brain_server.py) → FastAPI brain service
- [`robot_remote_agent.py`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/robot_remote_agent.py) → local remote-body client for GCP mode
- [`robot_face.py`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/robot_face.py) → terminal face
- [`quickstart.sh`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/quickstart.sh) → bootstrap plus run entrypoint; local by default, GCP with `--gcp`
- [`push.sh`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/push.sh) → public bootstrap entrypoint
- [`run.sh`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/run.sh) → public runtime entrypoint
- [`run_local.sh`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/run_local.sh) → local runtime helper
- [`run_gcp.sh`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/run_gcp.sh) → remote-brain GCP deployer with VM reuse support
- [`run_brain_server.sh`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/run_brain_server.sh) → remote brain startup script
- [`requirements_brain.txt`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/requirements_brain.txt) → minimal remote brain dependency set
- [`check_gcp.sh`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/check_gcp.sh) → GCP preflight helper
- [`GCP.md`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/GCP.md) → focused GCP notes
- [`.env.gcp.example`](/Users/aidevmac/Projects/Ephapsys/Product/ephapsys-sdk/samples/agents/robot/.env.gcp.example) → tracked GCP env template
