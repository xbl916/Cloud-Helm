#!/usr/bin/env bash
set -euo pipefail

version="${1:-0.3.0}"
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
release_root="$project_root/release"
stage_dir="$release_root/cloudhelm-$version"

mkdir -p "$release_root"
if [[ -e "$stage_dir" ]]; then
  echo "Release staging directory already exists: $stage_dir" >&2
  echo "Remove it explicitly before rebuilding this version." >&2
  exit 1
fi
mkdir -p "$stage_dir"

tar -C "$project_root" \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='agent.env' \
  --exclude='postgres.env' \
  --exclude='.venv' \
  --exclude='release' \
  --exclude='dist' \
  --exclude='build' \
  --exclude='*.egg-info' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  --exclude='.coverage' \
  -cf - . | tar -C "$stage_dir" -xf -

if compgen -G "$project_root/dist/cloud_helm-$version-*.whl" > /dev/null; then
  mkdir -p "$stage_dir/packages"
  cp "$project_root"/dist/cloud_helm-"$version"-*.whl "$stage_dir/packages/"
fi

archive="$release_root/cloudhelm-$version.tar.gz"
tar -C "$release_root" -czf "$archive" "cloudhelm-$version"
(cd "$release_root" && sha256sum "cloudhelm-$version.tar.gz" > "cloudhelm-$version.tar.gz.sha256")
echo "Created $archive"
echo "Created $archive.sha256"
