#!/usr/bin/env bash
# Build a Lambda-compatible dependency layer zip for Python 3.10 (x86_64).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$ROOT/.lambda_layer_build"
OUT_ZIP="$ROOT/swing-deps-layer.zip"

rm -rf "$BUILD_DIR" "$OUT_ZIP"
mkdir -p "$BUILD_DIR/python"

python3 -m pip install \
  -r "$ROOT/requirements-lambda.txt" \
  -t "$BUILD_DIR/python" \
  --upgrade

(
  cd "$BUILD_DIR"
  zip -r9 "$OUT_ZIP" python
)

echo "Created $OUT_ZIP"
echo "Upload this zip as a custom Lambda Layer, then attach:"
echo "  1) AWSSDKPandas-Python310 (AWS managed)"
echo "  2) your custom swing-deps-layer.zip"
echo "Handler: lambda_function.lambda_handler"
echo "Timeout: >= 5 minutes | Memory: >= 1024 MB recommended"
