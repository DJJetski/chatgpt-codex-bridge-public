#!/usr/bin/env sh
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"
source_mode=0
package_source=""
bootstrap_dir=""
bootstrap_cli=""

if [ -f "$repo_root/pyproject.toml" ] && [ -d "$repo_root/src/chatgpt_codex_bridge" ]; then
  source_mode=1
  package_source="."
  cd "$repo_root"
  PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
  export PYTHONPATH
else
  cd "$script_dir"
  for candidate in "$script_dir"/chatgpt_codex_bridge-*.whl "$script_dir"/chatgpt_codex_bridge-*.tar.gz; do
    if [ -f "$candidate" ]; then
      package_source="$candidate"
      break
    fi
  done
fi

python_bin="${PYTHON:-python3}"

if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 127
fi

"$python_bin" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python >= 3.11 is required")
PY

cleanup_bootstrap() {
  if [ -n "$bootstrap_dir" ]; then
    rm -rf "$bootstrap_dir"
  fi
}
trap cleanup_bootstrap EXIT

ensure_bootstrap_cli() {
  if [ -n "$bootstrap_cli" ]; then
    return
  fi
  if [ -z "$package_source" ]; then
    if command -v codex-bridge >/dev/null 2>&1; then
      bootstrap_cli="$(command -v codex-bridge)"
      return
    fi
    echo "install.sh must run from a source checkout or next to chatgpt_codex_bridge wheel/sdist artifacts" >&2
    exit 2
  fi
  bootstrap_dir="$(mktemp -d)"
  "$python_bin" -m venv "$bootstrap_dir/venv"
  "$bootstrap_dir/venv/bin/python" -m pip install --upgrade pip >/dev/null
  "$bootstrap_dir/venv/bin/python" -m pip install "$package_source" >/dev/null
  bootstrap_cli="$bootstrap_dir/venv/bin/codex-bridge"
}

run_bridge_cli() {
  if [ "$source_mode" -eq 1 ]; then
    "$python_bin" -m chatgpt_codex_bridge.cli "$@"
    return
  fi
  ensure_bootstrap_cli
  "$bootstrap_cli" "$@"
}

for arg in "$@"; do
  if [ "$arg" = "--dry-run" ]; then
    run_bridge_cli install "$@"
    exit $?
  fi
done

bridge_home_arg=""
expect_bridge_home=0
for arg in "$@"; do
  if [ "$expect_bridge_home" -eq 1 ]; then
    bridge_home_arg="$arg"
    expect_bridge_home=0
    continue
  fi
  case "$arg" in
    --bridge-home=*)
      bridge_home_arg="${arg#--bridge-home=}"
      ;;
    --bridge-home)
      expect_bridge_home=1
      ;;
  esac
done

if [ -z "$package_source" ]; then
  echo "install.sh must run from a source checkout or next to chatgpt_codex_bridge wheel/sdist artifacts" >&2
  exit 2
fi

if command -v uv >/dev/null 2>&1; then
  uv tool install --force "$package_source" >&2
elif command -v pipx >/dev/null 2>&1; then
  pipx install --force "$package_source" >&2
else
  if [ -n "$bridge_home_arg" ]; then
    bridge_home_for_venv="$bridge_home_arg"
  elif [ -n "${BRIDGE_HOME:-}" ]; then
    bridge_home_for_venv="$BRIDGE_HOME"
  else
    case "$(uname -s 2>/dev/null || echo unknown)" in
      Darwin)
        bridge_home_for_venv="$HOME/Library/Application Support/chatgpt-codex-bridge"
        ;;
      *)
        bridge_home_for_venv="${XDG_DATA_HOME:-$HOME/.local/share}/chatgpt-codex-bridge"
        ;;
    esac
  fi
  venv_dir="${BRIDGE_INSTALL_VENV:-$bridge_home_for_venv/install/venv}"
  mkdir -p "$(dirname "$venv_dir")"
  "$python_bin" -m venv "$venv_dir"
  . "$venv_dir/bin/activate"
  python -m pip install --upgrade pip >&2
  python -m pip install "$package_source" >&2
fi

run_bridge_cli install "$@"
