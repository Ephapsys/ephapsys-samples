#!/usr/bin/env bash
set -euo pipefail

BLUE="\033[36m"
GREEN="\033[32m"
YELLOW="\033[33m"
MAGENTA="\033[35m"
RESET="\033[0m"

info() {
  printf "${BLUE}[INFO]${RESET} %s\n" "$*"
}

warn() {
  printf "${YELLOW}[WARN]${RESET} %s\n" "$*" >&2
}

error() {
  printf "${MAGENTA}[ERROR]${RESET} %s\n" "$*" >&2
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

ensure_static_ip() {
  local region="${ZONE%-*}"
  local ip_name="${STATIC_IP_NAME:-${INSTANCE_PREFIX}-brain-ip}"
  STATIC_IP_NAME="$ip_name"

  if gcloud compute addresses describe "$ip_name" \
      --project="$PROJECT_ID" --region="$region" \
      --format="value(address)" >/dev/null 2>&1; then
    EXTERNAL_IP="$(gcloud compute addresses describe "$ip_name" \
      --project="$PROJECT_ID" --region="$region" --format="value(address)")"
    info "Reusing static IP $ip_name → $EXTERNAL_IP"
  else
    info "Reserving new static external IP: $ip_name (region=$region)"
    gcloud compute addresses create "$ip_name" \
      --project="$PROJECT_ID" --region="$region" --quiet
    EXTERNAL_IP="$(gcloud compute addresses describe "$ip_name" \
      --project="$PROJECT_ID" --region="$region" --format="value(address)")"
    info "Reserved $ip_name → $EXTERNAL_IP"
  fi
}

ensure_firewall_rule() {
  local rule_name="allow-${BRAIN_NETWORK_TAG}-ws"
  if ! gcloud compute firewall-rules describe "$rule_name" \
      --project="$PROJECT_ID" >/dev/null 2>&1; then
    info "Creating firewall rule $rule_name (tcp:${REMOTE_PORT} → tag:${BRAIN_NETWORK_TAG})"
    gcloud compute firewall-rules create "$rule_name" \
      --project="$PROJECT_ID" \
      --direction=INGRESS \
      --action=ALLOW \
      --rules="tcp:${REMOTE_PORT}" \
      --target-tags="$BRAIN_NETWORK_TAG" \
      --quiet
  else
    info "Firewall rule $rule_name already exists"
  fi
}
PYPROJECT="$REPO_ROOT/sdk/python/pyproject.toml"
META_FILE="$SCRIPT_DIR/.last_gcp_instance"
GCP_ENV_FILE="${GCP_ENV_FILE:-${ROBOT_GCP_ENV_FILE:-$SCRIPT_DIR/.env.gcp}}"
REUSE_INSTANCE=true
REUSED_EXISTING_INSTANCE=false
CONNECT_ONLY=false

usage() {
  cat <<'EOF'
Usage:
  ./run_gcp.sh
  ./run_gcp.sh --gpu
  ./run_gcp.sh --connect              # skip deploy, just connect to existing brain
  ./run_gcp.sh --static-ip-name my-brain-ip
  ./run_gcp.sh --fresh-instance

Notes:
  - Deploys only the Robot brain to GCP.
  - Keeps microphone, camera, speaker, and terminal face local.
  - Reserves a permanent static external IP (default: <INSTANCE_PREFIX>-brain-ip) and
    opens tcp:8765 via a firewall rule — no SSH tunnel needed.
  - Infrastructure and package-source settings are loaded from ./.env.gcp.
EOF
}

if [[ -f "$GCP_ENV_FILE" ]]; then
  info "Loading GCP settings from $GCP_ENV_FILE"
  set -a && source "$GCP_ENV_FILE" && set +a
fi

PROJECT_ID="${PROJECT_ID:-}"
ZONE="${ZONE:-}"
MACHINE_TYPE="${MACHINE_TYPE:-}"
DISK_SIZE="${DISK_SIZE:-}"
IMAGE_FAMILY="${IMAGE_FAMILY:-}"
IMAGE_PROJECT="${IMAGE_PROJECT:-}"
GPU_IMAGE_FAMILY="${GPU_IMAGE_FAMILY:-pytorch-2-7-cu128-ubuntu-2204-nvidia-570}"
INSTANCE_PREFIX="${INSTANCE_PREFIX:-}"
REMOTE_DIR="${REMOTE_DIR:-robot}"
REMOTE_PORT="${REMOTE_PORT:-8765}"
REMOTE_HOST="${REMOTE_HOST:-0.0.0.0}"
STATIC_IP_NAME="${STATIC_IP_NAME:-}"          # reserved static IP name; derived from INSTANCE_PREFIX if blank
BRAIN_NETWORK_TAG="robot-brain"
EXTERNAL_IP=""                                 # filled in by ensure_static_ip
CPU_ONLY=true
GPU_TYPE="${GPU_TYPE:-}"
GPU_COUNT="${GPU_COUNT:-1}"
GPU_MACHINE_TYPE="${GPU_MACHINE_TYPE:-}"
GPU_FALLBACKS="${GPU_FALLBACKS:-}"
ZONE_FALLBACKS="${ZONE_FALLBACKS:-}"
REGION_FALLBACKS="${REGION_FALLBACKS:-}"
USE_GPU="${USE_GPU:-0}"
SDK_PACKAGE_SOURCE="${SDK_PACKAGE_SOURCE:-${ROBOT_SDK_PACKAGE_SOURCE:-pypi}}"
SDK_INDEX_URL="${SDK_INDEX_URL:-${ROBOT_SDK_INDEX_URL:-}}"
SDK_EXTRA_INDEX_URL="${SDK_EXTRA_INDEX_URL:-${ROBOT_SDK_EXTRA_INDEX_URL:-}}"
case "${DISK_SIZE:-}" in
  *GB|*gb)
    disk_size_num="${DISK_SIZE%[Gg][Bb]}"
    if [[ "$disk_size_num" =~ ^[0-9]+$ && "$disk_size_num" -lt 100 ]]; then
      DISK_SIZE="100GB"
    fi
    ;;
esac

build_gpu_candidates() {
  local candidates=()
  local item_lc
  if [[ -n "$GPU_TYPE" && -n "$GPU_MACHINE_TYPE" ]]; then
    candidates+=("${GPU_TYPE}:${GPU_MACHINE_TYPE}:${GPU_COUNT}")
  fi
  if [[ -n "$GPU_FALLBACKS" ]]; then
    IFS=',' read -r -a fallback_items <<< "$GPU_FALLBACKS"
    for item in "${fallback_items[@]}"; do
      item="${item// /}"
      [[ -z "$item" ]] && continue
      if [[ "$item" == *:*:* ]]; then
        candidates+=("$item")
      elif [[ "$item" == *:* ]]; then
        candidates+=("${item}:${GPU_COUNT}")
      else
        item_lc="$(printf '%s' "$item" | tr '[:upper:]' '[:lower:]')"
        case "$item_lc" in
          t4) candidates+=("nvidia-tesla-t4:n1-standard-8:${GPU_COUNT}") ;;
          l4) candidates+=("nvidia-l4:g2-standard-8:${GPU_COUNT}") ;;
          v100) candidates+=("nvidia-tesla-v100:n1-standard-8:${GPU_COUNT}") ;;
          p100) candidates+=("nvidia-tesla-p100:n1-standard-8:${GPU_COUNT}") ;;
          a100) candidates+=("nvidia-tesla-a100:a2-highgpu-1g:${GPU_COUNT}") ;;
        esac
      fi
    done
  fi
  if [[ ${#candidates[@]} -eq 0 ]]; then
    candidates+=(
      "nvidia-tesla-t4:n1-standard-8:1"
      "nvidia-l4:g2-standard-8:1"
    )
  fi
  printf '%s\n' "${candidates[@]}" | awk '!seen[$0]++'
}

build_zone_candidates() {
  local zones=("$ZONE")
  local fallback_zones=()
  local regions=()
  local item region suffix

  if [[ -n "$ZONE_FALLBACKS" ]]; then
    IFS=',' read -r -a fallback_zones <<< "$ZONE_FALLBACKS"
    for item in "${fallback_zones[@]}"; do
      item="${item// /}"
      [[ -n "$item" ]] && zones+=("$item")
    done
  fi

  if [[ -n "$REGION_FALLBACKS" ]]; then
    IFS=',' read -r -a regions <<< "$REGION_FALLBACKS"
  else
    regions=("${ZONE%-*}")
  fi

  for region in "${regions[@]}"; do
    region="${region// /}"
    [[ -z "$region" ]] && continue
    for suffix in a b c d e f; do
      item="${region}-${suffix}"
      [[ "$item" != "$ZONE" ]] && zones+=("$item")
    done
  done

  printf '%s\n' "${zones[@]}" | awk '!seen[$0]++'
}

create_instance() {
  local zone="$1"
  local gpu_type="${2:-}"
  local machine_type="${3:-$MACHINE_TYPE}"
  local gpu_count="${4:-$GPU_COUNT}"
  local image_family="$IMAGE_FAMILY"
  local image_project="$IMAGE_PROJECT"

  if $CPU_ONLY; then
    info "Creating CPU VM $INSTANCE_NAME in $PROJECT_ID/$zone"
    gcloud compute instances create "$INSTANCE_NAME" \
      --project="$PROJECT_ID" \
      --zone="$zone" \
      --machine-type="$machine_type" \
      --boot-disk-size="$DISK_SIZE" \
      --image-family="$image_family" \
      --image-project="$image_project" \
      --address="$STATIC_IP_NAME" \
      --tags="$BRAIN_NETWORK_TAG" \
      --scopes=https://www.googleapis.com/auth/cloud-platform \
      --quiet
    return $?
  fi

  image_family="$GPU_IMAGE_FAMILY"
  image_project="deeplearning-platform-release"
  info "Creating GPU VM $INSTANCE_NAME in $PROJECT_ID/$zone (${gpu_type} on ${machine_type})"
  gcloud compute instances create "$INSTANCE_NAME" \
    --project="$PROJECT_ID" \
    --zone="$zone" \
    --machine-type="$machine_type" \
    --boot-disk-size="$DISK_SIZE" \
    --image-family="$image_family" \
    --image-project="$image_project" \
    --maintenance-policy=TERMINATE \
    --restart-on-failure \
    --accelerator="type=${gpu_type},count=${gpu_count}" \
    --metadata=install-nvidia-driver=True \
    --address="$STATIC_IP_NAME" \
    --tags="$BRAIN_NETWORK_TAG" \
    --scopes=https://www.googleapis.com/auth/cloud-platform \
    --quiet
}

select_instance_candidate() {
  local zones=()
  local gpus=()
  local zone candidate gpu_type machine_type gpu_count
  local total_zone total_gpu gpu_idx zone_idx

  while IFS= read -r zone; do
    [[ -n "$zone" ]] && zones+=("$zone")
  done < <(build_zone_candidates)

  if $CPU_ONLY; then
    total_zone=${#zones[@]}
    zone_idx=0
    for zone in "${zones[@]}"; do
      zone_idx=$((zone_idx + 1))
      info "Trying CPU candidate zone=${zone} (${zone_idx}/${total_zone})"
      if create_instance "$zone"; then
        ZONE="$zone"
        return 0
      fi
      warn "CPU candidate failed: zone=${zone}"
    done
    return 1
  fi

  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] && gpus+=("$candidate")
  done < <(build_gpu_candidates)

  total_gpu=${#gpus[@]}
  total_zone=${#zones[@]}
  gpu_idx=0
  for candidate in "${gpus[@]}"; do
    gpu_idx=$((gpu_idx + 1))
    IFS=':' read -r gpu_type machine_type gpu_count <<< "$candidate"
    [[ -z "$gpu_count" ]] && gpu_count="$GPU_COUNT"
    zone_idx=0
    for zone in "${zones[@]}"; do
      zone_idx=$((zone_idx + 1))
      info "Trying GPU candidate ${gpu_idx}/${total_gpu}, zone ${zone_idx}/${total_zone}: ${gpu_type} on ${machine_type} in ${zone}"
      if create_instance "$zone" "$gpu_type" "$machine_type" "$gpu_count"; then
        ZONE="$zone"
        GPU_TYPE="$gpu_type"
        GPU_MACHINE_TYPE="$machine_type"
        GPU_COUNT="$gpu_count"
        return 0
      fi
      warn "GPU candidate failed: ${gpu_type} on ${machine_type} in ${zone}"
    done
  done
  return 1
}

reuse_existing_instance() {
  if [ "$REUSE_INSTANCE" != true ] || [ ! -f "$META_FILE" ]; then
    return 1
  fi

  local saved_instance="" saved_project="" saved_zone="" saved_external_ip="" saved_remote_port="" saved_static_ip_name=""
  # shellcheck source=/dev/null
  source "$META_FILE"
  saved_instance="${INSTANCE_NAME:-}"
  saved_project="${PROJECT_ID:-}"
  saved_zone="${ZONE:-}"
  saved_external_ip="${EXTERNAL_IP:-}"
  saved_remote_port="${REMOTE_PORT:-}"
  saved_static_ip_name="${STATIC_IP_NAME:-}"

  if [[ -z "$saved_instance" || -z "$saved_project" || -z "$saved_zone" ]]; then
    return 1
  fi

  if [[ "$saved_project" != "$PROJECT_ID" ]]; then
    return 1
  fi

  if ! gcloud compute instances describe "$saved_instance" --project="$saved_project" --zone="$saved_zone" >/dev/null 2>&1; then
    return 1
  fi

  INSTANCE_NAME="$saved_instance"
  ZONE="$saved_zone"
  [[ -n "$saved_remote_port" ]] && REMOTE_PORT="$saved_remote_port"
  [[ -n "$saved_static_ip_name" ]] && STATIC_IP_NAME="$saved_static_ip_name"
  [[ -n "$saved_external_ip" ]] && EXTERNAL_IP="$saved_external_ip"
  REUSED_EXISTING_INSTANCE=true
  info "Reusing existing robot brain VM: instance=${INSTANCE_NAME} zone=${ZONE} project=${PROJECT_ID} ip=${EXTERNAL_IP}"
  return 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --zone) ZONE="$2"; shift 2 ;;
    --project) PROJECT_ID="$2"; shift 2 ;;
    --machine-type) MACHINE_TYPE="$2"; shift 2 ;;
    --disk-size) DISK_SIZE="$2"; shift 2 ;;
    --instance-prefix) INSTANCE_PREFIX="$2"; shift 2 ;;
    --static-ip-name) STATIC_IP_NAME="$2"; shift 2 ;;
    --remote-port) REMOTE_PORT="$2"; shift 2 ;;
    --connect) CONNECT_ONLY=true; shift ;;
    --reuse-instance) REUSE_INSTANCE=true; shift ;;
    --fresh-instance) REUSE_INSTANCE=false; shift ;;
    --gpu)
      CPU_ONLY=false
      shift
      ;;
    --gpu-type)
      CPU_ONLY=false
      GPU_TYPE="$2"
      shift 2
      ;;
    --gpu-count)
      CPU_ONLY=false
      GPU_COUNT="$2"
      shift 2
      ;;
    --gpu-machine-type)
      CPU_ONLY=false
      GPU_MACHINE_TYPE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      error "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ "$USE_GPU" == "1" || "$USE_GPU" == "true" || "$USE_GPU" == "yes" ]]; then
  CPU_ONLY=false
fi

if ! command -v gcloud >/dev/null 2>&1; then
  error "gcloud CLI not found. Install and authenticate first."
  exit 1
fi

"$SCRIPT_DIR/check_gcp.sh" >/dev/null

for var in PROJECT_ID ZONE MACHINE_TYPE DISK_SIZE IMAGE_FAMILY IMAGE_PROJECT INSTANCE_PREFIX; do
  if [[ -z "${!var:-}" ]]; then
    error "$var must be set in $GCP_ENV_FILE"
    exit 1
  fi
done

SDK_VERSION="$(PYPROJECT_PATH="$PYPROJECT" python3 - <<'PY'
import os, pathlib, re
text = pathlib.Path(os.environ["PYPROJECT_PATH"]).read_text()
match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
print(match.group(1) if match else "0.0.0")
PY
)"

if [[ "$SDK_VERSION" == "0.0.0" || -z "$SDK_VERSION" ]]; then
  error "Unable to read SDK version from $PYPROJECT"
  exit 1
fi

ENV_FILE_LOCAL="${GCP_RUNTIME_ENV_FILE:-$SCRIPT_DIR/.env}"
ACTIVE_ENV_FILE="$ENV_FILE_LOCAL"

if [[ ! -f "$ACTIVE_ENV_FILE" ]]; then
  error "Missing runtime env file: $ENV_FILE_LOCAL"
  exit 1
fi

set -a
source "$ACTIVE_ENV_FILE"
set +a

if [[ -z "${AOC_BASE_URL:-${AOC_API_URL:-}}" ]]; then
  error "AOC_BASE_URL or AOC_API_URL must be set in $ACTIVE_ENV_FILE"
  exit 1
fi
if [[ -z "${AOC_ORG_ID:-}" || -z "${AOC_PROVISIONING_TOKEN:-}" || -z "${AGENT_TEMPLATE_ID:-}" ]]; then
  error "AOC_ORG_ID, AOC_PROVISIONING_TOKEN, and AGENT_TEMPLATE_ID must be set in $ACTIVE_ENV_FILE"
  exit 1
fi

INSTANCE_NAME="${INSTANCE_PREFIX}-$(date +%s)"
TEMP_ENV_FILE="$(mktemp)"
TEMP_SRC="$(mktemp -d)"

cleanup() {
  rm -f "$TEMP_ENV_FILE" >/dev/null 2>&1 || true
  rm -rf "$TEMP_SRC" >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_for_remote_port() {
  local attempts="${1:-24}"
  local delay="${2:-5}"
  local i
  for ((i=1; i<=attempts; i++)); do
    if gcloud compute ssh "$INSTANCE_NAME" \
      --project="$PROJECT_ID" \
      --zone="$ZONE" \
      --command="ss -ltn '( sport = :${REMOTE_PORT} )' | grep -q LISTEN"; then
      return 0
    fi
    warn "Remote brain port ${REMOTE_PORT} not ready yet (attempt ${i}/${attempts}). Waiting ${delay}s..."
    sleep "$delay"
  done
  return 1
}


cp "$ACTIVE_ENV_FILE" "$TEMP_ENV_FILE"
cat >>"$TEMP_ENV_FILE" <<EOF
ROBOT_BODY_MODE=remote
ROBOT_BRAIN_HOST=0.0.0.0
ROBOT_BRAIN_PORT=${REMOTE_PORT}
ROBOT_ENABLE_LIVE_VISION=${ROBOT_ENABLE_LIVE_VISION:-1}
DISABLE_AUDIO=1
EOF

mkdir -p "$TEMP_SRC"
cp "$SCRIPT_DIR"/robot_*.py "$TEMP_SRC"/
cp "$SCRIPT_DIR"/run_brain_server.sh "$TEMP_SRC"/
cp "$SCRIPT_DIR"/requirements_brain.txt "$TEMP_SRC"/

ensure_static_ip
ensure_firewall_rule

if ! reuse_existing_instance; then
  if $CONNECT_ONLY; then
    error "No existing instance found in $META_FILE. Cannot use --connect without a deployed brain."
    exit 1
  fi
  if ! select_instance_candidate; then
    if $CPU_ONLY; then
      error "Unable to create a CPU VM in any configured zone."
    else
      error "Unable to create a GPU VM in any configured zone with any configured candidate."
      error "Tried candidates from GPU_TYPE/GPU_MACHINE_TYPE and GPU_FALLBACKS=${GPU_FALLBACKS:-<default>}"
    fi
    exit 1
  fi
fi

printf 'INSTANCE_NAME=%s\nPROJECT_ID=%s\nZONE=%s\nREMOTE_PORT=%s\nEXTERNAL_IP=%s\nSTATIC_IP_NAME=%s\n' \
  "$INSTANCE_NAME" "$PROJECT_ID" "$ZONE" "$REMOTE_PORT" "$EXTERNAL_IP" "$STATIC_IP_NAME" >"$META_FILE"

if $CONNECT_ONLY; then
  info "Skipping deploy — connecting to existing brain at ${EXTERNAL_IP}:${REMOTE_PORT}"
else

info "Preparing remote VM runtime"
gcloud compute ssh "$INSTANCE_NAME" \
  --project="$PROJECT_ID" \
  --zone="$ZONE" \
  --command="mkdir -p ~/${REMOTE_DIR} ~/.venvs/robot-brain && sudo env DEBIAN_FRONTEND=noninteractive apt-get update -y >/dev/null && sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-venv ffmpeg libsndfile1 build-essential python3-dev >/dev/null"

info "Uploading robot brain sample files"
gcloud compute scp --recurse "$TEMP_SRC/." "${INSTANCE_NAME}:~/${REMOTE_DIR}/" --project="$PROJECT_ID" --zone="$ZONE"
gcloud compute scp "$TEMP_ENV_FILE" "${INSTANCE_NAME}:~/${REMOTE_DIR}/.env" --project="$PROJECT_ID" --zone="$ZONE"
gcloud compute ssh "$INSTANCE_NAME" \
  --project="$PROJECT_ID" \
  --zone="$ZONE" \
  --command="chmod +x ~/${REMOTE_DIR}/run_brain_server.sh"

SDK_PACKAGE_SOURCE_LC="$(printf '%s' "$SDK_PACKAGE_SOURCE" | tr '[:upper:]' '[:lower:]')"
case "$SDK_PACKAGE_SOURCE_LC" in
  pypi)
    REMOTE_PIP_INSTALL_CMD='python -m pip install "ephapsys[audio,vision,embedding]=='"$SDK_VERSION"'" >/dev/null'
    ;;
  testpypi)
    REMOTE_PIP_INSTALL_CMD='python -m pip install --extra-index-url https://pypi.org/simple --index-url https://test.pypi.org/simple "ephapsys[audio,vision,embedding]=='"$SDK_VERSION"'" >/dev/null'
    ;;
  local)
    mkdir -p "$TEMP_SRC/sdk-python-src"
    cp -R "$REPO_ROOT/sdk/python/." "$TEMP_SRC/sdk-python-src/"
    REMOTE_PIP_INSTALL_CMD='python -m pip install "$HOME/'"$REMOTE_DIR"'/sdk-python-src[audio,vision,embedding]" >/dev/null'
    ;;
  custom)
    if [[ -z "$SDK_INDEX_URL" ]]; then
      error "SDK_INDEX_URL must be set when SDK_PACKAGE_SOURCE=custom"
      exit 1
    fi
    CUSTOM_INDEX_FLAGS="--index-url \"$SDK_INDEX_URL\""
    if [[ -n "$SDK_EXTRA_INDEX_URL" ]]; then
      CUSTOM_INDEX_FLAGS="$CUSTOM_INDEX_FLAGS --extra-index-url \"$SDK_EXTRA_INDEX_URL\""
    fi
    REMOTE_PIP_INSTALL_CMD='python -m pip install '"$CUSTOM_INDEX_FLAGS"' "ephapsys[audio,vision,embedding]=='"$SDK_VERSION"'" >/dev/null'
    ;;
  *)
    error "Unsupported SDK_PACKAGE_SOURCE value"
    exit 1
    ;;
esac

cat >"$TEMP_SRC/install_remote.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
rm -rf ~/.venvs/robot-brain
python3 -m venv ~/.venvs/robot-brain
source ~/.venvs/robot-brain/bin/activate
python -m pip install --upgrade pip >/dev/null
python -m pip install --upgrade --force-reinstall --no-cache-dir \
  --index-url https://download.pytorch.org/whl/cu128 \
  torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1 >/dev/null
${REMOTE_PIP_INSTALL_CMD}
python -m pip install --upgrade --force-reinstall --no-cache-dir -r ~/${REMOTE_DIR}/requirements_brain.txt >/dev/null
python - <<'PY'
from pathlib import Path
import certifi
import torch
from transformers import AutoTokenizer
from transformers.models.yolos.modeling_yolos import YolosForObjectDetection

ca_bundle = Path(certifi.where())
if not ca_bundle.is_file():
    raise SystemExit(f"certifi CA bundle missing: {ca_bundle}")

if not torch.cuda.is_available():
    raise SystemExit("torch.cuda.is_available() is false after cu128 install")

_ = AutoTokenizer
_ = YolosForObjectDetection
print("robot brain dependency smoke test passed")
PY
EOF
chmod +x "$TEMP_SRC/install_remote.sh"
gcloud compute scp "$TEMP_SRC/install_remote.sh" "${INSTANCE_NAME}:~/${REMOTE_DIR}/install_remote.sh" --project="$PROJECT_ID" --zone="$ZONE"

info "Installing remote Python dependencies"
gcloud compute ssh "$INSTANCE_NAME" \
  --project="$PROJECT_ID" \
  --zone="$ZONE" \
  --command="bash ~/${REMOTE_DIR}/install_remote.sh"

if ! $CPU_ONLY; then
  info "Verifying remote GPU runtime"
  if ! gcloud compute ssh "$INSTANCE_NAME" \
    --project="$PROJECT_ID" \
    --zone="$ZONE" \
    --command="bash -lc 'command -v nvidia-smi >/dev/null 2>&1 && source ~/.venvs/robot-brain/bin/activate && python -c \"import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)\"'"; then
    error "Remote GPU runtime is not usable on ${INSTANCE_NAME}. The guest OS is missing a working NVIDIA driver/CUDA stack."
    error "nvidia-smi is unavailable or torch cannot see CUDA. Recreate the VM with a GPU-ready image or install drivers in the guest first."
    exit 1
  fi
fi

cat >"$TEMP_SRC/start_remote.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd ~/${REMOTE_DIR}
source ~/.venvs/robot-brain/bin/activate

# Reuse mode must replace the existing brain process, not stack another uvicorn
# instance on the same loopback port.
pkill -f "uvicorn robot_brain_server:app --host ${REMOTE_HOST} --port ${REMOTE_PORT}" >/dev/null 2>&1 || true
pkill -f "python3 -m uvicorn robot_brain_server:app --host ${REMOTE_HOST} --port ${REMOTE_PORT}" >/dev/null 2>&1 || true
sleep 1

nohup ./run_brain_server.sh > ~/robot_brain.log 2>&1 < /dev/null &
EOF
chmod +x "$TEMP_SRC/start_remote.sh"
gcloud compute scp "$TEMP_SRC/start_remote.sh" "${INSTANCE_NAME}:~/${REMOTE_DIR}/start_remote.sh" --project="$PROJECT_ID" --zone="$ZONE"

info "Starting remote robot brain"
gcloud compute ssh "$INSTANCE_NAME" \
  --project="$PROJECT_ID" \
  --zone="$ZONE" \
  --command="bash ~/${REMOTE_DIR}/start_remote.sh"

info "Waiting for remote brain to listen on ${REMOTE_PORT}"
if ! wait_for_remote_port 24 5; then
  error "Remote brain did not become ready on port ${REMOTE_PORT}. Check ~/robot_brain.log on the VM."
  exit 1
fi

info "Robot brain is live at ${EXTERNAL_IP}:${REMOTE_PORT}"
printf 'INSTANCE_NAME=%s\nPROJECT_ID=%s\nZONE=%s\nREMOTE_PORT=%s\nEXTERNAL_IP=%s\nSTATIC_IP_NAME=%s\n' \
  "$INSTANCE_NAME" "$PROJECT_ID" "$ZONE" "$REMOTE_PORT" "$EXTERNAL_IP" "$STATIC_IP_NAME" >"$META_FILE"

fi  # end of deploy section (skipped by --connect)

info "Launching local body + terminal face against remote brain"
export ROBOT_BRAIN_WS_URL="ws://${EXTERNAL_IP}:${REMOTE_PORT}/ws/state"
export ROBOT_BRAIN_AUDIO_WS_URL="ws://${EXTERNAL_IP}:${REMOTE_PORT}/ws/body/audio"
export ROBOT_BRAIN_VIDEO_WS_URL="ws://${EXTERNAL_IP}:${REMOTE_PORT}/ws/body/video"
export ROBOT_BRAIN_CONTROL_WS_URL="ws://${EXTERNAL_IP}:${REMOTE_PORT}/ws/body/control"
python3 "$SCRIPT_DIR/robot_remote_agent.py"
