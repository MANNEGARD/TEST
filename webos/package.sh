#!/usr/bin/env bash
# Paketerar SMGD Hem som webOS-app (.ipk) och, om så önskas, installerar den på TV:n.
#   ./package.sh            bygger se.smgd.hem_<version>_all.ipk
#   ./package.sh tv         bygger och installerar på enheten "tv" (se ares-setup-device)
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v ares-package >/dev/null 2>&1; then
  echo "ares-package saknas. Installera webOS CLI:  npm install -g @webos-tools/cli" >&2
  exit 1
fi
out="$(mktemp -d)"
ares-package . --outdir "$out" --no-minify -e package.sh -e README.md -e config.example.json -e .gitignore
ipk="$(ls "$out"/*.ipk)"
cp "$ipk" .
echo "Byggt: $(basename "$ipk")"
if [ "${1:-}" != "" ]; then
  ares-install --device "$1" "$(basename "$ipk")"
  ares-launch --device "$1" se.smgd.hem
fi
