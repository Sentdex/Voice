#!/usr/bin/env bash
# Fetch the Kokoro ONNX model and voice pack into models/kokoro/.
#
# Works on both Linux and macOS: curl is preferred because it ships with macOS,
# and wget is used when curl is unavailable. Both are invoked so that an
# interrupted download resumes instead of starting over.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="${ROOT_DIR}/models/kokoro"
RELEASE_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"

if command -v curl >/dev/null 2>&1; then
  download() {
    curl --fail --location --retry 3 --continue-at - --output "$2" "$1"
  }
elif command -v wget >/dev/null 2>&1; then
  download() {
    wget --continue --output-document "$2" "$1"
  }
else
  echo "Neither curl nor wget is available; install one of them first." >&2
  exit 1
fi

mkdir -p "${MODEL_DIR}"

for asset in kokoro-v1.0.onnx voices-v1.0.bin; do
  echo "Fetching ${asset}"
  download "${RELEASE_URL}/${asset}" "${MODEL_DIR}/${asset}"
done

echo "Kokoro assets installed in ${MODEL_DIR}"
