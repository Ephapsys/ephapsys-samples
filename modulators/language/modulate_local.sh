#!/usr/bin/env bash
# ============================================================
# Bash wrapper to run the Ephaptic Language Trainer
# ============================================================

set -euo pipefail

MODE="${1:-run}" # run | smoke
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info() {
  echo "[INFO] $*"
}

error() {
  echo "[ERROR] $*" >&2
}

ensure_runtime_env() {
  local venv sdk_extras sdk_source sdk_version
  venv="${MODULATOR_VENV:-.venv}"
  sdk_extras="${MODULATOR_SDK_EXTRAS:-modulation}"
  sdk_source="${MODULATOR_SDK_PACKAGE_SOURCE:-${SDK_PACKAGE_SOURCE:-pypi}}"
  sdk_version="${MODULATOR_SDK_VERSION:-${SDK_VERSION:-}}"

  # Always use a dedicated venv to avoid version conflicts with system packages
  # Prefer Python 3.12 — datasets/dill is incompatible with Python 3.14
  local py_bin="python3"
  for candidate in python3.12 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      py_bin="$candidate"
      break
    fi
  done
  if [ ! -d "$venv" ]; then
    info "Creating virtualenv at $venv (using $py_bin)"
    "$py_bin" -m venv "$venv"
  fi
  # shellcheck disable=SC1090
  source "$venv/bin/activate"

  if python3 -c "import ephapsys" >/dev/null 2>&1 && [ "$sdk_source" != "testpypi" ]; then
    info "Ephapsys SDK already installed in venv"
  else
    local pip_args="--quiet"
    local pkg="ephapsys[${sdk_extras}]"
    if [ -n "$sdk_version" ]; then
      pkg="ephapsys[${sdk_extras}]==${sdk_version}"
    fi

    case "$sdk_source" in
      pypi)
        ;;
      testpypi)
        pip_args="$pip_args --extra-index-url https://pypi.org/simple --index-url https://test.pypi.org/simple"
        ;;
      local)
        # Install from local SDK source tree (for development)
        local sdk_dir="${MODULATOR_SDK_LOCAL_PATH:-$(cd "$SCRIPT_DIR" && cd "../../../ephapsys-sdk/sdk/python" 2>/dev/null && pwd)}"
        if [ -d "$sdk_dir" ]; then
          info "Installing Ephapsys SDK from local source: $sdk_dir"
          PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install --upgrade pip $pip_args
          PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install -e "${sdk_dir}[${sdk_extras}]" $pip_args
          return
        else
          error "Local SDK not found at $sdk_dir — falling back to pypi"
        fi
        ;;
      *)
        error "Unsupported SDK_PACKAGE_SOURCE: $sdk_source (use pypi, testpypi, or local)"
        exit 1
        ;;
    esac

    info "Installing Ephapsys SDK from $sdk_source"
    PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install --upgrade pip $pip_args
    PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install "$pkg" $pip_args

    if ! python3 -c "import ephapsys" >/dev/null 2>&1; then
      error "SDK installation failed. Check your Python environment."
      exit 1
    fi
  fi

  if [ -f "requirements.txt" ]; then
    info "Syncing modulator-local requirements into $venv"
    PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install -r requirements.txt --quiet
  fi

  # Install torch==2.6.0+cu124 LAST and with --force-reinstall.
  #
  # Why: ephapsys[modulation,audio,vision,embedding,eval] pulls torch as a
  # transitive dep (default cu126/cu128 wheel from PyPI), then requirements.txt
  # pulls torch-adjacent packages that may downgrade torch's CUDA libs.
  # The default cu126/128 wheel falls back to CPU on Lambda Cloud's CUDA
  # 12.0 driver. Symptom: A100 training at ~20 sec/step instead of <1.
  #
  # We install LAST + --force-reinstall (but WITH deps — don't use --no-deps,
  # that drops libcusparseLt/nvidia-nccl-cu12 etc. which torch needs at
  # runtime). Pip resolves all CUDA libs from the cu124 index for us.
  # Pin torch + torchvision TOGETHER from the cu124 index. requirements.txt
  # pulls torchvision unpinned (latest = 0.26.0 built against torch 2.11),
  # which becomes ABI-incompatible the moment we force-reinstall torch back
  # to 2.6.0 → "RuntimeError: operator torchvision::nms does not exist".
  # PyTorch's compatibility matrix: torch 2.6.0 ↔ torchvision 0.21.0.
  info "Installing torch==2.6.0 + torchvision==0.21.0 (cu124, matches Lambda Stack's driver)"
  PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install --force-reinstall \
    torch==2.6.0 torchvision==0.21.0 \
    --index-url https://download.pytorch.org/whl/cu124 --quiet

  # Sanity check: warn loudly if CUDA isn't actually working after install.
  # Better to flag the CPU fallback now than to let the user pay $1.99/hr
  # for an A100 doing CPU work for hours before noticing.
  if ! python3 -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
    info "⚠️  WARNING: torch.cuda.is_available() = False after install."
    info "⚠️  Training will run on CPU — ~20× slower than A100 GPU."
    info "⚠️  If you see ~20 sec/step in trial output, that's why. Check"
    info "⚠️  Lambda Stack's NVIDIA driver compatibility with the torch wheel."
    # Diagnostic. Use a heredoc to avoid shell-escaping quotes.
    python3 <<'PYDIAG' 2>&1 | sed 's/^/[INFO] ⚠️  diagnostic: /'
import torch, sys
print(f"torch={torch.__version__} cuda_built_against={torch.version.cuda}")
try:
    print(f"cuda.is_available={torch.cuda.is_available()}")
    print(f"cuda.device_count={torch.cuda.device_count()}")
except Exception as e:
    print(f"cuda probe raised: {e}", file=sys.stderr)
PYDIAG
  else
    info "✓ CUDA working: $(python3 -c 'import torch; print(torch.cuda.get_device_name(0))')"
  fi
}

ensure_python_deps() {
  if python3 -c "import pkg_resources" >/dev/null 2>&1; then
    return
  fi
  info "Installing local Python bootstrap dependency: setuptools<74"
  python3 -m pip install 'setuptools<74'
  python3 -c "import pkg_resources" >/dev/null 2>&1 || {
    error "pkg_resources is still unavailable after installing setuptools<74"
    exit 1
  }
}

# --- Load environment from .env if present ---
if [ -f ".env" ]; then
  info "Loading environment from .env"
  # shellcheck disable=SC2046
  export $(grep -v '^#' .env | xargs)
fi

# --- Default values ---
BASE_URL=${AOC_BASE_URL:-${BASE_URL:-"http://localhost:7001"}}
AOC_ORG_ID=${AOC_ORG_ID:-""}
AOC_MODULATION_TOKEN=${AOC_MODULATION_TOKEN:-""}
MODEL_TEMPLATE_ID=${MODEL_TEMPLATE_ID:-""}
OUTDIR=${OUTDIR:-"./artifacts"}
TRAIN_MODE=${TRAIN_MODE:-"1"}   # ✅ Default = 1 (training enabled)

# --- Sanity checks ---
if [ -z "$AOC_ORG_ID" ] || [ -z "$AOC_MODULATION_TOKEN" ] || [ -z "$MODEL_TEMPLATE_ID" ]; then
  error "AOC_ORG_ID, AOC_MODULATION_TOKEN and MODEL_TEMPLATE_ID must be set."
  exit 1
fi

if [ "$MODE" = "smoke" ] || [ "${SAMPLE_CI_SMOKE:-0}" = "1" ]; then
  echo "[CI][smoke] Language modulator env validation OK."
  echo "[CI][smoke] BASE_URL=$BASE_URL TEMPLATE=$MODEL_TEMPLATE_ID"
  ensure_runtime_env
  python3 -m py_compile train_language.py
  echo "[CI][smoke] train_language.py syntax OK."
  exit 0
fi

ensure_runtime_env
ensure_python_deps

info "Starting Ephaptic Language Trainer (model=${MODEL_TEMPLATE_ID})"

# --- Build Python command dynamically ---
CMD=(
  python3 train_language.py
  --base_url "$BASE_URL"
  --api_key "$AOC_MODULATION_TOKEN"
  --model_template_id "$MODEL_TEMPLATE_ID"
  --outdir "$OUTDIR"
)

if [ "$TRAIN_MODE" = "1" ]; then
  CMD+=(--train)
else
  info "Evaluation-only mode (baseline + ephaptic comparison)"
fi

# --- Execute trainer ---
exec "${CMD[@]}"
