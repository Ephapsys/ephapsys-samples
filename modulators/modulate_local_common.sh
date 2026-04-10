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

  if python3 -c "import ephapsys" >/dev/null 2>&1 && [ "$sdk_source" != "testpypi" ]; then
    modulator_info "Ephapsys SDK already installed, skipping install"
  else
    if [ ! -d "$venv" ]; then
      modulator_info "Creating virtualenv at $venv"
      python3 -m venv "$venv"
    fi
    # shellcheck disable=SC1090
    source "$venv/bin/activate"

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

  if [ -f "$MODULATOR_COMMON_DIR/requirements.gcp.txt" ]; then
    modulator_info "Syncing shared modulator requirements into $venv"
    PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install -r "$MODULATOR_COMMON_DIR/requirements.gcp.txt" --quiet
  fi

  if [ -f "requirements.txt" ]; then
    modulator_info "Syncing modulator-local requirements into $venv"
    PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install -r requirements.txt --quiet
  fi
}
