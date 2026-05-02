#!/usr/bin/env bash

set -euo pipefail

MODULATOR_COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

modulator_info() {
  echo "[INFO] $*"
}

modulator_error() {
  echo "[ERROR] $*" >&2
}

modulator_load_env_file() {
  local env_file line key value current_value
  env_file="${1:-.env}"

  if [ ! -f "$env_file" ]; then
    return
  fi

  modulator_info "Loading defaults from $env_file"
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      ''|'#'*)
        continue
        ;;
    esac
    if [[ "$line" != *=* ]]; then
      continue
    fi
    key="${line%%=*}"
    value="${line#*=}"
    current_value="${!key-}"
    if [ -n "$current_value" ]; then
      continue
    fi
    printf -v "$key" '%s' "$value"
    export "$key"
  done < "$env_file"
}

modulator_prepare_env() {
  local venv sdk_extras sdk_source sdk_version pip_args pkg
  venv="${MODULATOR_VENV:-.venv}"
  sdk_extras="${MODULATOR_SDK_EXTRAS:-modulation,audio,vision,embedding,eval}"
  sdk_source="${MODULATOR_SDK_PACKAGE_SOURCE:-${SDK_PACKAGE_SOURCE:-pypi}}"
  sdk_version="${MODULATOR_SDK_VERSION:-${SDK_VERSION:-}}"

  if [ "${MODULATOR_SKIP_SDK_SETUP:-0}" = "1" ]; then
    modulator_info "Using pre-provisioned Python environment (MODULATOR_SKIP_SDK_SETUP=1)"
    if ! python3 -c "import ephapsys" >/dev/null 2>&1; then
      modulator_error "Pre-provisioned environment does not provide the ephapsys package."
      exit 1
    fi
    return
  fi

  # Always use a dedicated venv to avoid version conflicts with system packages
  if [ ! -d "$venv" ]; then
    modulator_info "Creating virtualenv at $venv"
    python3 -m venv "$venv"
  fi
  # shellcheck disable=SC1090
  source "$venv/bin/activate"

  if python3 -c "import ephapsys" >/dev/null 2>&1 && [ "$sdk_source" != "testpypi" ]; then
    modulator_info "Ephapsys SDK already installed in venv"
  else
    pip_args="--quiet"
    pkg="ephapsys[${sdk_extras}]"
    if [ -n "$sdk_version" ]; then
      pkg="ephapsys[${sdk_extras}]==${sdk_version}"
    fi

    case "$sdk_source" in
      pypi)
        ;;
      testpypi)
        pip_args="$pip_args --extra-index-url https://pypi.org/simple --index-url https://test.pypi.org/simple"
        ;;
      *)
        modulator_error "Unsupported SDK_PACKAGE_SOURCE: $sdk_source (use pypi or testpypi)"
        exit 1
        ;;
    esac

    modulator_info "Installing Ephapsys SDK from $sdk_source"
    PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install --upgrade pip $pip_args
    PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install "$pkg" $pip_args

    if ! python3 -c "import ephapsys" >/dev/null 2>&1; then
      modulator_error "SDK installation failed. Check your Python environment."
      exit 1
    fi
  fi

  # Install torch with the cu124 wheel BEFORE the requirements.gcp.txt
  # install — otherwise pip pulls torch's default cu126/cu128 wheel from
  # PyPI (built against CUDA 12.6/12.8 toolkit), which silently falls back
  # to CPU on Lambda Cloud's CUDA 12.0 driver. The cu124 wheel runs fine on
  # the older driver. Symptom of the bug: A100 instance running training at
  # ~20s/step instead of <1s/step (full Phase 1 = 30+ hours = $60+ wasted).
  # See modulate_lambda_common.sh which uses the same pin in its outer venv.
  if ! python3 -c "import torch; assert '+cu124' in torch.__version__ or torch.cuda.is_available()" 2>/dev/null; then
    modulator_info "Installing torch==2.6.0+cu124 (matches Lambda Stack's CUDA 12.x driver)"
    PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install \
      torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124 --quiet
  fi

  if [ -f "$MODULATOR_COMMON_DIR/requirements.gcp.txt" ]; then
    modulator_info "Syncing shared modulator requirements into $venv"
    PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install -r "$MODULATOR_COMMON_DIR/requirements.gcp.txt" --quiet
  fi

  # Sanity check: warn if CUDA isn't actually working after install
  if ! python3 -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
    modulator_info "WARNING: torch.cuda.is_available() = False — training will run on CPU (~20× slower than GPU). Check NVIDIA driver compatibility with the installed torch wheel."
  fi

  if [ -f "requirements.txt" ]; then
    modulator_info "Syncing modulator-local requirements into $venv"
    PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install -r requirements.txt --quiet
  fi
}
