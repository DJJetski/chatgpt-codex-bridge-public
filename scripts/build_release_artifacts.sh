#!/usr/bin/env sh
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/.." && pwd)"
dist_dir="${1:-$repo_root/dist}"

cd "$repo_root"

python_bin="${PYTHON:-python3}"
build_env=""

cleanup() {
  if [ -n "$build_env" ]; then
    rm -rf "$build_env"
  fi
  rm -rf build *.egg-info src/*.egg-info
}
trap cleanup EXIT

"$python_bin" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python >= 3.11 is required")
PY

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if [ "${BRIDGE_RELEASE_ALLOW_DIRTY:-0}" != "1" ] && [ -n "$(git status --short --untracked-files=normal)" ]; then
    echo "Release artifacts must be built from a clean Git checkout. Commit or remove local changes first." >&2
    exit 2
  fi
fi

rm -rf build "$dist_dir" *.egg-info src/*.egg-info
mkdir -p "$dist_dir"

build_env="$(mktemp -d)"
"$python_bin" -m venv "$build_env/venv"
"$build_env/venv/bin/python" -m pip install --upgrade pip
"$build_env/venv/bin/python" -m pip install build
"$build_env/venv/bin/python" -m build --outdir "$dist_dir"

ref_name="${GITHUB_REF_NAME:-$(git describe --tags --always --dirty 2>/dev/null || echo local)}"
git archive --format=tar.gz --prefix="chatgpt-codex-bridge-${ref_name}/" -o "$dist_dir/chatgpt-codex-bridge-${ref_name}-source.tar.gz" HEAD
cp scripts/install.sh "$dist_dir/install.sh"

"$python_bin" scripts/check_release_artifacts.py "$dist_dir"/*
(cd "$dist_dir" && shasum -a 256 * > SHA256SUMS && shasum -a 256 -c SHA256SUMS)
