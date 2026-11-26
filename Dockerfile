FROM public.ecr.aws/lambda/python:3.11

# LightGBM requires OpenMP (libgomp) on Amazon Linux
RUN if command -v dnf >/dev/null 2>&1; then \
        dnf install -y libgomp && dnf clean all; \
    else \
        yum install -y libgomp && yum clean all; \
    fi

# Install CPU-only PyTorch first so stable-baselines3 does not pull CUDA libraries
COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Copy application source
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
