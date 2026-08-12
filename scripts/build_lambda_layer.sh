#!/usr/bin/env bash
# Build a Lambda-compatible dependency layer zip for Python 3.12 (x86_64).
# Pair with AWS managed layer AWSSDKPandas-Python312 for pandas/numpy.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$ROOT/.lambda_layer_build"
OUT_ZIP="$ROOT/swing-deps-layer.zip"
REQ_FILE="$ROOT/requirements-lambda.txt"
PY_BIN="${PYTHON_BIN:-python3.12}"

if ! command -v "$PY_BIN" >/dev/null 2>&1; then
  echo "ERROR: $PY_BIN not found. Install Python 3.12 (e.g. brew install python@3.12)."
  exit 1
fi

rm -rf "$BUILD_DIR" "$OUT_ZIP"
mkdir -p "$BUILD_DIR/python"

# Split git vs binary requirements (only-binary cannot install from git)
BIN_REQ="$(mktemp)"
GIT_REQ="$(mktemp)"
trap 'rm -f "$BIN_REQ" "$GIT_REQ"' EXIT

grep -v '^#' "$REQ_FILE" | grep -v '^$' | grep -v 'git+' > "$BIN_REQ" || true
grep 'git+' "$REQ_FILE" > "$GIT_REQ" || true

echo "Using $($PY_BIN --version)"
echo "Installing binary manylinux2014_x86_64 wheels for Python 3.12..."
if [ -s "$BIN_REQ" ]; then
  "$PY_BIN" -m pip install \
    -r "$BIN_REQ" \
    -t "$BUILD_DIR/python" \
    --upgrade \
    --platform manylinux2014_x86_64 \
    --implementation cp \
    --python-version 3.12 \
    --only-binary=:all:
fi

echo "Installing git/source packages (tvdatafeed)..."
if [ -s "$GIT_REQ" ]; then
  # Source packages are arch-independent; install without platform pins.
  "$PY_BIN" -m pip install \
    -r "$GIT_REQ" \
    -t "$BUILD_DIR/python" \
    --upgrade \
    --no-deps
fi

# Strip packages provided by the Lambda runtime / AWSSDKPandas managed layer
echo "Stripping pandas/numpy/botocore (provided by AWS layers/runtime)..."
(
  cd "$BUILD_DIR/python"
  rm -rf \
    pandas pandas-* \
    numpy numpy-* \
    numpy.libs \
    pyarrow pyarrow-* \
    boto3 boto3-* \
    botocore botocore-* \
    s3transfer s3transfer-* \
    __pycache__
  find . -type d -name "__pycache__" -prune -exec rm -rf {} +
)

SIZE_MB=$(du -sm "$BUILD_DIR/python" | awk '{print $1}')
echo "Unzipped layer size: ${SIZE_MB} MB"

if [ "$SIZE_MB" -gt 240 ]; then
  echo "WARNING: layer is close to/over the 250 MB Lambda unzipped limit."
fi

(
  cd "$BUILD_DIR"
  zip -r9q "$OUT_ZIP" python
)

echo "Created $OUT_ZIP ($(du -h "$OUT_ZIP" | awk '{print $1}'))"
echo
echo "Upload this zip as a custom Lambda Layer, then attach:"
echo "  1) AWSSDKPandas-Python312 (AWS managed)"
echo "  2) your custom swing-deps-layer.zip"
echo "Handler: lambda_function.lambda_handler"
echo "Runtime: python3.12 | Architecture: x86_64"
