#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
#  modulate_lambda_common.sh
#  Lambda-cloud counterpart to modulate_gcp_common.sh
# ════════════════════════════════════════════════════════════════
#
#  Provisions a Lambda Cloud GPU, copies the modulator sample +
#  trainer onto it, runs the trainer (which talks to AOC via the
#  ephapsys SDK to do Bayesian search and report results), pulls
#  artifacts back, and terminates the instance.
#
#  Mirrors the structure of modulate_gcp_common.sh so push.sh's
#  --mode lambda path drops in cleanly alongside --mode gcp.
#
#  Required environment (typically loaded from samples/.env):
#    BASE_URL                 AOC backend (https://api.staging.ephapsys.ai)
#    AOC_ORG_ID               Org id (org_…)
#    AOC_MODULATION_TOKEN     Modulation API token (mod_…)
#    MODEL_TEMPLATE_ID        Model template id from AOC
#    LAMBDA_API_KEY           Lambda Cloud API key (secret_api_…)
#    LAMBDA_SSH_KEY_NAME      Name of the SSH key registered in Lambda
#    LAMBDA_SSH_KEY_PATH      Local path to the matching .pem file
#
#  Optional:
#    LAMBDA_INSTANCE_TYPES    Comma-separated fallback list (default
#                             gpu_1x_a100,gpu_1x_a100_sxm4,gpu_1x_a10)
#    LAMBDA_API_BASE          API base URL (defaults to public endpoint)
#    SDK_PACKAGE_SOURCE       pypi | testpypi
#    SDK_VERSION              Pinned SDK version (auto-detected from local env)
#    AUTO_DELETE              true|false — terminate on exit (default true)
#    REMOTE_BASE_DIR          Where to install on the instance (default ~/ephapsys-modulators)
#
set -euo pipefail

BLUE="\033[36m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[91m"
RESET="\033[0m"

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Shared Lambda Cloud helpers (lambda_api, launch, wait_active, wait_ssh, terminate).
# Also used by agents/helloworld/run_lambda.sh for the persistent-VM runtime path.
# shellcheck source=lib/lambda.sh
source "$COMMON_DIR/lib/lambda.sh"
MODULATOR_DIR="${MODULATOR_DIR:-$(pwd)}"
MODULATOR_KIND="${MODULATOR_KIND:-$(basename "$MODULATOR_DIR")}"
TRAINER_SCRIPT="${TRAINER_SCRIPT:-train_${MODULATOR_KIND}.py}"
DEFAULT_OUTDIR="${DEFAULT_OUTDIR:-./artifacts_${MODULATOR_KIND}}"

if [ ! -d "$MODULATOR_DIR" ]; then
  echo "❌ MODULATOR_DIR does not exist: $MODULATOR_DIR"
  exit 1
fi
if [ ! -f "$MODULATOR_DIR/$TRAINER_SCRIPT" ]; then
  echo "❌ Trainer script not found: $MODULATOR_DIR/$TRAINER_SCRIPT"
  exit 1
fi

# ── Load .env (samples convention) ──────────────────────────────
load_env_file() {
  local env_file="$1"
  local key value current
  [ -f "$env_file" ] || return 0
  echo "📂 Loading defaults from $env_file"
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      ''|'#'*) continue ;;
    esac
    line="${line#export }"
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    # Strip surrounding quotes if present
    value="${value%\"}"
    value="${value#\"}"
    current="${!key-}"
    if [ -n "$current" ]; then
      continue
    fi
    printf -v "$key" '%s' "$value"
    export "$key"
  done < "$env_file"
}

[ -f "$MODULATOR_DIR/.env" ] && load_env_file "$MODULATOR_DIR/.env"
[ -f "$COMMON_DIR/lambda.env" ] && load_env_file "$COMMON_DIR/lambda.env"

# ── Resolve config ──────────────────────────────────────────────
BASE_URL="${AOC_BASE_URL:-${BASE_URL:-}}"
AOC_ORG_ID="${AOC_ORG_ID:-}"
AOC_MODULATION_TOKEN="${AOC_MODULATION_TOKEN:-}"
MODEL_TEMPLATE_ID="${MODEL_TEMPLATE_ID:-}"
HF_TOKEN="${HF_TOKEN:-}"

LAMBDA_API_BASE="${LAMBDA_API_BASE:-https://cloud.lambdalabs.com/api/v1}"
LAMBDA_API_KEY="${LAMBDA_API_KEY:-}"
LAMBDA_SSH_KEY_NAME="${LAMBDA_SSH_KEY_NAME:-}"
LAMBDA_SSH_KEY_PATH="${LAMBDA_SSH_KEY_PATH:-}"
LAMBDA_INSTANCE_TYPES="${LAMBDA_INSTANCE_TYPES:-gpu_1x_a100,gpu_1x_a100_sxm4,gpu_1x_a10}"

# AOC creds — must be set
if [ -z "$BASE_URL" ] || [ -z "$AOC_ORG_ID" ] || [ -z "$AOC_MODULATION_TOKEN" ] || [ -z "$MODEL_TEMPLATE_ID" ]; then
  echo "❌ BASE_URL/AOC_ORG_ID/AOC_MODULATION_TOKEN/MODEL_TEMPLATE_ID missing in .env"
  exit 1
fi

# Lambda creds — must be set
if [ -z "$LAMBDA_API_KEY" ] || [ -z "$LAMBDA_SSH_KEY_NAME" ] || [ -z "$LAMBDA_SSH_KEY_PATH" ]; then
  echo "❌ LAMBDA_API_KEY / LAMBDA_SSH_KEY_NAME / LAMBDA_SSH_KEY_PATH must be set"
  echo "   Generate API key at: https://cloud.lambdalabs.com/api-keys"
  echo "   Register an SSH key in the Lambda dashboard, then save the .pem locally."
  exit 1
fi

if [ ! -f "$LAMBDA_SSH_KEY_PATH" ]; then
  echo "❌ SSH key not found at: $LAMBDA_SSH_KEY_PATH"
  exit 1
fi

command -v jq >/dev/null || { echo "❌ jq not installed (brew install jq)"; exit 1; }

# ── Resolve SDK source/version ─────────────────────────────────
# Default: install LATEST published version from PyPI on the worker.
# Experiments must be reproducible across machines, so we deliberately
# do NOT auto-detect from the local install (that pinned reproducibility
# to whatever version happened to be sitting in the user's venv — which
# burned an A100 on 2026-04-30 when local was at 0.2.73 but the worker
# needed 0.2.79's compute_indispensability_loss).
#
# To pin a specific version (e.g. for paper-canonical reproduction),
# set HELLOWORLD_SDK_VERSION in .env or export SDK_VERSION beforehand.
SDK_PACKAGE_SOURCE="${MODULATOR_SDK_PACKAGE_SOURCE:-${SDK_PACKAGE_SOURCE:-pypi}}"
SDK_VERSION="${HELLOWORLD_SDK_VERSION:-${SDK_VERSION:-}}"
if [ -n "$SDK_VERSION" ]; then
  echo "📦 SDK pinned to ephapsys==${SDK_VERSION} (via HELLOWORLD_SDK_VERSION/SDK_VERSION)"
else
  echo "📦 SDK: installing latest from PyPI on the worker (no version pin)"
fi

REMOTE_BASE_DIR="${REMOTE_BASE_DIR:-~/ephapsys-modulators}"
REMOTE_DIR="${REMOTE_BASE_DIR}/${MODULATOR_KIND}"
EXPERIMENT_TAG="${EXPERIMENT_TAG:-${MODULATOR_KIND}-modulate}"
AUTO_DELETE="${AUTO_DELETE:-true}"
TRAIN_MODE="${TRAIN_MODE:-1}"
RUN_TS="$(date +"%Y%m%d_%H%M%S")"
RESULTS_DIR="$MODULATOR_DIR/results/${EXPERIMENT_TAG}_lambda_${RUN_TS}"
mkdir -p "$RESULTS_DIR"
TEMP_SRC="$(mktemp -d)"

info()    { printf "${BLUE}[INFO]${RESET} %s\n" "$*"; }
success() { printf "${GREEN}[SELECTED]${RESET} %s\n" "$*"; }
warn()    { printf "${YELLOW}[WARN]${RESET} %s\n" "$*" >&2; }
error()   { printf "${RED}[ERROR]${RESET} %s\n" "$*" >&2; }

MASKED="${AOC_MODULATION_TOKEN:0:8}********"
echo "🔐 Env loaded: BASE_URL=$BASE_URL, AOC_ORG_ID=$AOC_ORG_ID, AOC_MODULATION_TOKEN=${MASKED}, MODEL_TEMPLATE_ID=$MODEL_TEMPLATE_ID"

# ── Capacity-aware launch (helpers provided by lib/lambda.sh) ──
# Locally-named globals kept for backward compat with downstream code in this
# file (cleanup, log lines, etc.) that references INSTANCE_ID / LAUNCHED_TYPE
# / LAUNCHED_REGION / HOST. We mirror the LAMBDA_* outputs from the lib into
# these names so the rest of the script needs no rename.
INSTANCE_ID=""
LAUNCHED_TYPE=""
LAUNCHED_REGION=""
HOST=""

cleanup() {
  rm -rf "$TEMP_SRC" >/dev/null 2>&1 || true
  if [ -z "$INSTANCE_ID" ]; then
    return 0
  fi
  if [ "$AUTO_DELETE" = "true" ]; then
    warn "Terminating Lambda instance: $INSTANCE_ID ($LAUNCHED_TYPE in $LAUNCHED_REGION)"
    lambda_terminate "$INSTANCE_ID"
  else
    warn "AUTO_DELETE=false — leaving instance running: $INSTANCE_ID"
  fi
}
trap cleanup EXIT

LAMBDA_INSTANCE_NAME="${LAMBDA_INSTANCE_NAME:-ephapsys-${MODULATOR_KIND}-modulate-$(date +%Y%m%d-%H%M%S)}"
if ! lambda_launch_instance "$LAMBDA_INSTANCE_TYPES" "LAMBDA_INSTANCE_TYPES" "$LAMBDA_INSTANCE_NAME"; then
  error "Could not launch any Lambda instance — all configured types/regions exhausted."
  exit 1
fi
INSTANCE_ID="$LAMBDA_INSTANCE_ID"
LAUNCHED_TYPE="$LAMBDA_LAUNCHED_TYPE"
LAUNCHED_REGION="$LAMBDA_LAUNCHED_REGION"

# ── Wait for instance active + SSH ready ────────────────────────
if ! lambda_wait_active "$INSTANCE_ID"; then
  error "Instance never became active"
  exit 1
fi
HOST="$LAMBDA_HOST"
success "Instance active at $HOST"

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$LAMBDA_SSH_KEY_PATH")
SSH_CMD=(ssh "${SSH_OPTS[@]}" "ubuntu@${HOST}")
SCP_CMD=(scp "${SSH_OPTS[@]}")

lambda_wait_ssh "$HOST" "$LAMBDA_SSH_KEY_PATH"

# ── Verify GPU + apt deps (Lambda Stack already has CUDA + drivers) ──
echo "⚙️  Verifying GPU + installing apt deps..."
"${SSH_CMD[@]}" 'nvidia-smi --query-gpu=name --format=csv,noheader; python3 --version'
"${SSH_CMD[@]}" 'sudo apt-get update -qq >/dev/null 2>&1 && sudo apt-get install -qq -y python3-venv ffmpeg libsndfile1 git >/dev/null 2>&1; echo done'

# ── Stage local files into a clean rsync source ─────────────────
mkdir -p "$TEMP_SRC/common" "$TEMP_SRC/modulator"
rsync -a --delete \
  --exclude '.env*' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude 'artifacts' \
  --exclude 'out' \
  --exclude 'results' \
  --exclude '.docker_build' \
  "$MODULATOR_DIR/" "$TEMP_SRC/modulator/"
[ -f "$COMMON_DIR/modulate_local_common.sh" ] && cp "$COMMON_DIR/modulate_local_common.sh" "$TEMP_SRC/common/"
# Reuse GCP requirements file — same Python deps, hardware-agnostic.
[ -f "$COMMON_DIR/requirements.gcp.txt" ] && cp "$COMMON_DIR/requirements.gcp.txt" "$TEMP_SRC/common/requirements.lambda.txt"

# ── Upload to instance ──────────────────────────────────────────
echo "📂 Copying modulator sample files to instance..."
"${SSH_CMD[@]}" "mkdir -p $REMOTE_BASE_DIR $REMOTE_DIR"
"${SCP_CMD[@]}" -r -C "$TEMP_SRC/modulator/." "ubuntu@${HOST}:$REMOTE_DIR/" 2>/dev/null
[ -f "$TEMP_SRC/common/modulate_local_common.sh" ] && \
  "${SCP_CMD[@]}" -C "$TEMP_SRC/common/modulate_local_common.sh" "ubuntu@${HOST}:$REMOTE_BASE_DIR/modulate_local_common.sh" 2>/dev/null
[ -f "$TEMP_SRC/common/requirements.lambda.txt" ] && \
  "${SCP_CMD[@]}" -C "$TEMP_SRC/common/requirements.lambda.txt" "ubuntu@${HOST}:$REMOTE_BASE_DIR/requirements.lambda.txt" 2>/dev/null
[ -f "$MODULATOR_DIR/.env" ] && \
  "${SCP_CMD[@]}" -C "$MODULATOR_DIR/.env" "ubuntu@${HOST}:$REMOTE_DIR/.env" 2>/dev/null

# ── Optional: upload an external dataset file referenced by ────
# AOC_DATASET_PATH so AOC's Bayesian search trains on the same
# data the downstream long training will use. The local path
# pointed at by AOC_DATASET_PATH is uploaded into REMOTE_DIR/data/
# on the worker, and AOC_DATASET_PATH is rewritten in the worker's
# .env to the on-instance path before train_language.py reads it.
if [ -n "${AOC_DATASET_PATH:-}" ] && [ -f "$AOC_DATASET_PATH" ]; then
  echo "📦 Uploading dataset file to instance: $AOC_DATASET_PATH"
  ds_basename="$(basename "$AOC_DATASET_PATH")"
  "${SSH_CMD[@]}" "mkdir -p $REMOTE_DIR/data"
  "${SCP_CMD[@]}" -C "$AOC_DATASET_PATH" "ubuntu@${HOST}:$REMOTE_DIR/data/$ds_basename" 2>/dev/null
  # Rewrite the path in the modulator's .env to point at the on-worker copy.
  # CRITICAL: expand ~ to /home/ubuntu before writing — Python doesn't
  # expand tilde from env vars, so HF datasets sees ~ as a relative path
  # and prepends cwd, producing nonsense like "/cwd/~/path/file.jsonl".
  # Lambda Cloud always uses ubuntu user, so /home/ubuntu is safe to hardcode.
  remote_path="${REMOTE_DIR/#\~/\/home\/ubuntu}/data/$ds_basename"
  "${SSH_CMD[@]}" "
    if grep -q '^AOC_DATASET_PATH=' $REMOTE_DIR/.env 2>/dev/null; then
      sed -i 's|^AOC_DATASET_PATH=.*|AOC_DATASET_PATH=$remote_path|' $REMOTE_DIR/.env
    else
      echo 'AOC_DATASET_PATH=$remote_path' >> $REMOTE_DIR/.env
    fi
  "
  echo "  → AOC_DATASET_PATH on worker: $remote_path"
fi

# ── Build setup + run scripts (avoid SSH quoting hell) ──────────
SDK_PACKAGE_SOURCE_LC="$(printf '%s' "$SDK_PACKAGE_SOURCE" | tr '[:upper:]' '[:lower:]')"
REMOTE_SETUP_SCRIPT="$(mktemp "${TEMP_SRC}/setup_remote.XXXXXX.sh")"
REMOTE_RUN_SCRIPT="$(mktemp "${TEMP_SRC}/run_remote.XXXXXX.sh")"

# Setup: clean venv (no --system-site-packages, see security paper EXPERIMENTS.md
# for rationale — Lambda Stack's system PyTorch is built against numpy 1.x and
# its system Pillow lacks Resampling, both incompatible with transformers >= 5.x)
cat > "$REMOTE_SETUP_SCRIPT" <<SETUP_EOF
#!/usr/bin/env bash
set -e
python3 -m venv ~/.venvs/ephapsys-modulator
source ~/.venvs/ephapsys-modulator/bin/activate
python -m pip install --upgrade pip >/dev/null
# torch matched to Lambda Stack's CUDA 12.x driver; latest torch defaults
# pull cu128/cu13 wheels that silently fall back to CPU on this driver.
python -m pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124 >/dev/null
SETUP_EOF

SDK_SPEC="ephapsys[modulation,audio,vision,embedding,eval]"
[ -n "$SDK_VERSION" ] && SDK_SPEC="${SDK_SPEC}==${SDK_VERSION}"

case "$SDK_PACKAGE_SOURCE_LC" in
  pypi)
    echo "python -m pip install '${SDK_SPEC}' >/dev/null" >> "$REMOTE_SETUP_SCRIPT"
    # Use importlib.metadata — SDK 0.2.79+ doesn't expose ephapsys.__version__
    # directly. importlib.metadata works for any pip-installed package.
    echo "python -c 'from importlib.metadata import version; print(\"  installed ephapsys\", version(\"ephapsys\"))'" >> "$REMOTE_SETUP_SCRIPT"
    ;;
  testpypi)
    echo "python -m pip install --extra-index-url https://pypi.org/simple --index-url https://test.pypi.org/simple '${SDK_SPEC}' >/dev/null" >> "$REMOTE_SETUP_SCRIPT"
    # Use importlib.metadata — SDK 0.2.79+ doesn't expose ephapsys.__version__
    # directly. importlib.metadata works for any pip-installed package.
    echo "python -c 'from importlib.metadata import version; print(\"  installed ephapsys\", version(\"ephapsys\"))'" >> "$REMOTE_SETUP_SCRIPT"
    ;;
  *)
    error "Unsupported SDK package source for Lambda modulation: $SDK_PACKAGE_SOURCE"
    exit 1
    ;;
esac

cat >> "$REMOTE_SETUP_SCRIPT" <<SETUP_EOF2
if [ -f ${REMOTE_BASE_DIR}/requirements.lambda.txt ]; then
  python -m pip install -r ${REMOTE_BASE_DIR}/requirements.lambda.txt >/dev/null
fi
if [ -f ${REMOTE_DIR}/requirements.txt ]; then
  python -m pip install -r ${REMOTE_DIR}/requirements.txt >/dev/null
fi
SETUP_EOF2

# Run script: activate venv + dispatch to the modulator's local runner.
cat > "$REMOTE_RUN_SCRIPT" <<RUN_EOF
#!/usr/bin/env bash
set -e
export TRAIN_MODE=${TRAIN_MODE}
cd ${REMOTE_DIR}
source ~/.venvs/ephapsys-modulator/bin/activate
export MODULATOR_SKIP_SDK_SETUP=1
chmod +x ./*.sh ../modulate_local_common.sh >/dev/null 2>&1 || true
if [ -f ./modulate.sh ]; then
  exec ./modulate.sh
elif [ -f ./modulate_local.sh ]; then
  exec ./modulate_local.sh
else
  echo "❌ No modulate.sh or modulate_local.sh in ${REMOTE_DIR}"
  exit 1
fi
RUN_EOF

"${SCP_CMD[@]}" -C "$REMOTE_SETUP_SCRIPT" "ubuntu@${HOST}:${REMOTE_BASE_DIR}/setup_remote.sh" 2>/dev/null
"${SCP_CMD[@]}" -C "$REMOTE_RUN_SCRIPT" "ubuntu@${HOST}:${REMOTE_BASE_DIR}/run_remote.sh" 2>/dev/null

# ── Install + run ───────────────────────────────────────────────
echo "📦 Installing remote Python environment..."
"${SSH_CMD[@]}" "bash -l ${REMOTE_BASE_DIR}/setup_remote.sh"

echo "🚀 Running $MODULATOR_KIND modulator remotely on Lambda..."
# Run the worker FIRST in its own SSH call so its exit code propagates.
# Previous form chained `; ls -lhR ... || true` after the worker, which
# always returned 0 and silently masked worker crashes (e.g. ImportError
# from a stale SDK). On 2026-04-30 this caused push.sh to report success
# while the worker had crashed before writing summary.json — Phase 1 then
# trained on 11-day-old hyperparameters because the picker fell back to
# a stale file. Fail loud here instead so push.sh can exit non-zero.
WORKER_RC=0
"${SSH_CMD[@]}" -t "bash -l ${REMOTE_BASE_DIR}/run_remote.sh" || WORKER_RC=$?
# Diagnostic listing (always runs so we can postmortem partial results).
"${SSH_CMD[@]}" "ls -lhR $REMOTE_DIR/artifacts* $REMOTE_DIR/out $REMOTE_DIR/results 2>/dev/null" || true
if [[ "$WORKER_RC" -ne 0 ]]; then
  warn "Worker exited with code $WORKER_RC — pulling whatever it managed to write for postmortem"
fi

# ── Pull artifacts back ─────────────────────────────────────────
# CRITICAL cost fix (2026-04-30): the previous "scp -r artifacts*" pulled
# back ~3.5GB of model files (model_snapshot.zip + base_model/) over a
# single-stream SCP pipe. On a cross-country link that took 5+ hours,
# during which the A100 was billing at $1.99/hr — wasting ~$11 per run
# downloading data we don't need (the modulator's job is to produce
# summary.json + curves, not ship the model around).
#
# We use rsync over ssh to:
#   - exclude the large *.safetensors and *.zip files
#   - do the multi-pass file walk efficiently
#   - support resume on partial transfers
# rsync over ssh is ~3-5x faster than single-stream scp for many small files,
# and for this workload (mostly small JSON + a few PNGs) cuts download from
# 5+ hours to <1 minute.
echo "📥 Copying results locally (excluding model snapshots)..."
# Critical: rsync doesn't expand ~ or shell globs in remote paths the way
# scp does. Two prior bugs from this:
#   - "~/ephapsys-modulators/..." treated as literal → rsync 404s silently
#   - "artifacts*" glob never resolved → no matching files to transfer
# Fix: expand tilde to /home/ubuntu (Lambda always uses ubuntu user) AND
# rsync each subdir explicitly with trailing slash to copy CONTENTS into
# RESULTS_DIR/<subdir>/. Don't swallow errors — we want to see failures.
RSYNC_OPTS=(-az --partial
            --exclude='*.safetensors'
            --exclude='*.safetensors.*'
            --exclude='model_snapshot.zip'
            --exclude='base_model'
            --exclude='vanilla_model'
            --exclude='*.bin')
# Use --info=progress2 only if local rsync supports it (3.1+). macOS ships
# rsync 2.6.9 (2006) which only knows --progress. Detect once and slot in.
if rsync --info=progress2 --version >/dev/null 2>&1; then
  RSYNC_OPTS+=(--info=progress2)
elif rsync --progress --version >/dev/null 2>&1; then
  RSYNC_OPTS+=(--progress)
fi
RSYNC_SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i $LAMBDA_SSH_KEY_PATH"
REMOTE_DIR_ABS="${REMOTE_DIR/#\~/\/home\/ubuntu}"

for remote_subdir in artifacts out results; do
  # Probe whether the directory exists on the worker before invoking rsync,
  # otherwise rsync prints scary "No such file or directory" warnings.
  if "${SSH_CMD[@]}" "[ -d $REMOTE_DIR_ABS/$remote_subdir ]" 2>/dev/null; then
    mkdir -p "$RESULTS_DIR/$remote_subdir"
    echo "  → rsync $remote_subdir/"
    rsync "${RSYNC_OPTS[@]}" -e "$RSYNC_SSH" \
      "ubuntu@${HOST}:$REMOTE_DIR_ABS/$remote_subdir/" "$RESULTS_DIR/$remote_subdir/" \
      || warn "rsync of $remote_subdir failed (continuing — other subdirs may have results)"
  fi
done

# ── Terminate the GPU NOW (cost-leak fix; matches the security paper's
# Lambda branch behavior — disarm trap so cleanup doesn't double-fire) ──
cleanup
trap - EXIT
INSTANCE_ID=""

if [[ "$WORKER_RC" -ne 0 ]]; then
  echo "❌ Worker failed (exit $WORKER_RC). Partial results (if any) under $RESULTS_DIR"
  exit "$WORKER_RC"
fi

echo "✅ Results saved in $RESULTS_DIR"
