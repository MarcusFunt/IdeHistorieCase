#!/usr/bin/env bash
# Optional M1 starter model (Qwen3.5-4B Q4_K_M). Downloads only when invoked.
set -euo pipefail
MODELS_DIR=${1:-"$PWD/models"}
NAME="Qwen3.5-4B-Q4_K_M.gguf"
REVISION="b0bd07786f94b700012140fd9263b2de83d4f97b"
URL="https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/$REVISION/$NAME"
SHA256="00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4"
mkdir -p "$MODELS_DIR"
MODELS_DIR=$(cd "$MODELS_DIR" && pwd)
TARGET="$MODELS_DIR/$NAME"
if [[ ! -f "$TARGET" ]]; then
  echo "Downloading $NAME (~2.74 GB) from a pinned Hugging Face revision..."
  curl --fail --location --retry 3 --continue-at - --output "$TARGET.part" "$URL"
  printf '%s  %s\n' "$SHA256" "$TARGET.part" | sha256sum --check --status || {
    echo "SHA256 check failed; the partial model has not been installed." >&2
    exit 1
  }
  mv "$TARGET.part" "$TARGET"
fi
printf '%s  %s\n' "$SHA256" "$TARGET" | sha256sum --check
echo "Set LLM_MODELS_DIR=$MODELS_DIR"
echo "Set LLM_GGUF_FILE=$NAME"
