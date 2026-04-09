#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAMPLE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$SAMPLE_DIR"

ECM_PATH="$(find .ephapsys_state -name 'ecm.pt' -print | head -n 1)"

if [[ -z "${ECM_PATH}" ]]; then
  echo "[ERROR] No cached ecm.pt found under $SAMPLE_DIR/.ephapsys_state" >&2
  echo "[INFO] Run ./run.sh once successfully first so the runtime cache exists." >&2
  exit 1
fi

BACKUP_PATH="$(mktemp -t helloworld-ecm-backup)"
cp "$ECM_PATH" "$BACKUP_PATH"

restore_ecm() {
  if [[ -f "$BACKUP_PATH" ]]; then
    cp "$BACKUP_PATH" "$ECM_PATH"
    rm -f "$BACKUP_PATH"
    echo "[INFO] Restored $ECM_PATH"
  fi
}

trap restore_ecm EXIT

echo "[INFO] Backed up $ECM_PATH to $BACKUP_PATH"
printf 'CORRUPTED_ECM!!!' | dd of="$ECM_PATH" bs=1 seek=0 conv=notrunc status=none
echo "[INFO] Corrupted $ECM_PATH"
echo "[INFO] Running ./run.sh with corrupted ECM cache"

./run.sh
