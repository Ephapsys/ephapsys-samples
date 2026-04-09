#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GCP_ENV_FILE="${GCP_ENV_FILE:-${ROBOT_GCP_ENV_FILE:-$SCRIPT_DIR/.env.gcp}}"
if [[ -f "$GCP_ENV_FILE" ]]; then
  set -a && source "$GCP_ENV_FILE" && set +a
fi

if [[ -n "${ZONE:-}" ]]; then
  export REGION="${ZONE%-*}"
fi

if [[ -n "${DISK_SIZE:-}" ]]; then
  case "$DISK_SIZE" in
    *GB|*gb)
      disk_size_num="${DISK_SIZE%[Gg][Bb]}"
      if [[ "$disk_size_num" =~ ^[0-9]+$ && "$disk_size_num" -lt 100 ]]; then
        export DISK_SIZE="100GB"
      fi
      ;;
  esac
fi

if [[ -z "${ROBOT_GCP_MODULATION_GPU:-}" ]]; then
  if [[ -n "${GPU_FALLBACKS:-}" ]]; then
    IFS=',' read -r first_gpu _ <<< "$GPU_FALLBACKS"
    export ROBOT_GCP_MODULATION_GPU="${first_gpu// /}"
  elif [[ -n "${GPU_TYPE:-}" ]]; then
    case "${GPU_TYPE}" in
      nvidia-tesla-t4) export ROBOT_GCP_MODULATION_GPU="t4" ;;
      nvidia-l4) export ROBOT_GCP_MODULATION_GPU="l4" ;;
      nvidia-tesla-v100) export ROBOT_GCP_MODULATION_GPU="v100" ;;
      nvidia-tesla-p100) export ROBOT_GCP_MODULATION_GPU="p100" ;;
      *) export ROBOT_GCP_MODULATION_GPU="t4" ;;
    esac
  else
    export ROBOT_GCP_MODULATION_GPU="t4"
  fi
fi

cat <<EOF
[INFO] Robot GCP mode keeps body + terminal local and moves heavy work off the laptop.
[INFO] GCP target defaults come from ${GCP_ENV_FILE}.
[INFO] Full modulation will reserve a GPU VM first, copy the modulator samples there, and pip install ephapsys on the VM (gpu=${ROBOT_GCP_MODULATION_GPU}).
[INFO] Idempotent mode also prefers the same VM-first modulate_gcp.sh path so ephaptic coupling remains exercised in GCP mode.
EOF

export ROBOT_MODULATION_MODE="gcp"
exec ./push_local.sh "$@"
