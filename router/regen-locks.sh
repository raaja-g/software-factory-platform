#!/bin/sh
# Regenerates router/torch.lock and router/requirements.lock (run from the repo root).
set -eu
cd "$(dirname "$0")"
uv pip compile torch.in --universal --python-version 3.11 --generate-hashes --no-deps \
  --index-url https://download.pytorch.org/whl/cpu -o torch.lock
# torch (and the CUDA stack it drags in on Linux) comes from torch.lock, never from PyPI.
uv pip compile requirements.in --universal --python-version 3.11 --generate-hashes \
  --index-url https://pypi.org/simple \
  $(for p in torch triton cuda-bindings cuda-pathfinder cuda-toolkit nvidia-cublas nvidia-cuda-cupti \
      nvidia-cuda-nvrtc nvidia-cuda-runtime nvidia-cudnn-cu13 nvidia-cufft nvidia-cufile nvidia-curand \
      nvidia-cusolver nvidia-cusparse nvidia-cusparselt-cu13 nvidia-nccl-cu13 nvidia-nvjitlink \
      nvidia-nvshmem-cu13 nvidia-nvtx; do printf -- '--no-emit-package %s ' "$p"; done) \
  -o requirements.lock
