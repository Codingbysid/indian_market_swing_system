FROM public.ecr.aws/lambda/python:3.11

# LightGBM requires OpenMP (libgomp) on Amazon Linux, plus gcc toolchain for extensions
RUN if command -v dnf >/dev/null 2>&1; then \
        dnf install -y libgomp gcc gcc-c++ && dnf clean all; \
    else \
        yum install -y libgomp gcc gcc-c++ && yum clean all; \
    fi

COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --prefer-binary -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Copy configuration, frozen production V3 models, and self-contained source
COPY config/ ${LAMBDA_TASK_ROOT}/config/
COPY v2/models/latest/ ${LAMBDA_TASK_ROOT}/v2/models/latest/
COPY src/ ${LAMBDA_TASK_ROOT}/src/

# Prevent thread contention and route writable caches to /tmp (Lambda's only writable dir)
ENV PYTHONPATH="${LAMBDA_TASK_ROOT}" \
    OMP_NUM_THREADS="1" \
    OPENBLAS_NUM_THREADS="1" \
    MKL_NUM_THREADS="1" \
    NUMBA_CACHE_DIR="/tmp" \
    MPLCONFIGDIR="/tmp" \
    JOBLIB_TEMP_FOLDER="/tmp"

CMD ["src.aws.lambda_handler.lambda_handler"]
