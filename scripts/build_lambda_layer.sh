#!/usr/bin/env bash
# Build a Lambda-compatible dependency layer zip for Python 3.10 (x86_64).
# Uses manylinux wheels so the zip matches AWS Lambda (works on macOS ARM hosts).
# Pair with AWS managed layer AWSSDKPandas-Python310 for pandas/numpy.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$ROOT/.lambda_layer_build"
OUT_ZIP="$ROOT/swing-deps-layer.zip"

rm -rf "$BUILD_DIR" "$OUT_ZIP"
mkdir -p "$BUILD_DIR/python"

echo "Installing manylinux2014_x86_64 wheels for Python 3.10..."
python3 -m pip install \
  -r "$ROOT/requirements-lambda.txt" \
  -t "$BUILD_DIR/python" \
  --upgrade \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.10 \
  --only-binary=:all:

# Strip packages provided by the Lambda runtime / AWSSDKPandas managed layer
# so the custom layer stays under the 250 MB unzipped limit.
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
  find . -type d -name "*.dist-info" -prune -exec rm -rf {} + 2>/dev/null || true
)

# Keep dist-info for installed custom packages; only remove stripped ones above.
# Re-install is fine; dist-info cleanup of remaining packages can break imports of metadata.

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
echo "  1) AWSSDKPandas-Python310 (AWS managed)"
echo "  2) your custom swing-deps-layer.zip"
echo "Handler: lambda_function.lambda_handler"
echo "Timeout: >= 5 minutes | Memory: >= 1024 MB recommended"
echo "Architecture: x86_64"
echo
echo "If console upload rejects the zip (>50 MB), upload to S3 first:"
echo "  aws s3 cp swing-deps-layer.zip s3://indian-swing-bot-data-2026/layers/"
echo "  then create the layer from that S3 object."
