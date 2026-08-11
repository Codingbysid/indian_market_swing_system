#!/usr/bin/env bash
# Build a Lambda-compatible dependency layer zip for Python 3.10 (x86_64).
# Prefer Docker (Amazon Linux) so wheels match AWS Lambda; fall back to local pip.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$ROOT/.lambda_layer_build"
OUT_ZIP="$ROOT/swing-deps-layer.zip"

rm -rf "$BUILD_DIR" "$OUT_ZIP"
mkdir -p "$BUILD_DIR/python"

if command -v docker >/dev/null 2>&1; then
  echo "Building layer with Docker (public.ecr.aws/lambda/python:3.10)..."
  docker run --rm --platform linux/amd64 \
    -v "$ROOT":/var/task \
    -w /var/task \
    public.ecr.aws/lambda/python:3.10 \
    /bin/bash -c "pip install -r requirements-lambda.txt -t .lambda_layer_build/python --upgrade"
else
  echo "Docker not found; installing with manylinux pip flags (may fail on macOS)..."
  python3 -m pip install \
    -r "$ROOT/requirements-lambda.txt" \
    -t "$BUILD_DIR/python" \
    --upgrade \
    --platform manylinux2014_x86_64 \
    --implementation cp \
    --python-version 3.10 \
    --only-binary=:all:
fi

(
  cd "$BUILD_DIR"
  zip -r9 "$OUT_ZIP" python
)

echo "Created $OUT_ZIP ($(du -h "$OUT_ZIP" | awk '{print $1}'))"
echo "Upload this zip as a custom Lambda Layer, then attach:"
echo "  1) AWSSDKPandas-Python310 (AWS managed)"
echo "  2) your custom swing-deps-layer.zip"
echo "Handler: lambda_function.lambda_handler"
echo "Timeout: >= 5 minutes | Memory: >= 1024 MB recommended"
