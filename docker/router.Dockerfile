# Laya router sidecar (M11.1). Hash-locked installs, weights baked at a pinned revision and verified,
# no network needed at run time. Built for the host arch; the same file builds amd64 and arm64.
#   docker buildx build --platform linux/amd64,linux/arm64 -f docker/router.Dockerfile .
# Bump the base digest, locks, revision and weight hashes together: see router/README.md.
FROM python:3.11-slim-trixie@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY router/torch.lock router/requirements.lock router/weights.sha256 /router/
# Two hash-locked installs, one index each, so no package can resolve from the wrong index.
RUN pip install --require-hashes --no-deps --index-url https://download.pytorch.org/whl/cpu -r /router/torch.lock \
 && pip install --require-hashes --no-deps --index-url https://pypi.org/simple -r /router/requirements.lock

# Weights: safetensors and json only (never pickles), at a pinned commit, verified file by file.
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('convaiinnovations/laya', revision='fd4f9dbcf4fde222fd43174bdd4c3b08b789e4e1', allow_patterns=['*.safetensors', '*.json', '*.txt'], local_dir='/opt/laya-weights')" \
 && cd /opt/laya-weights && sha256sum -c /router/weights.sha256 \
 && rm -rf /opt/laya-weights/.cache

COPY router/shim.py /router/shim.py
RUN useradd -u 10001 -d /home/laya -m laya \
 && mkdir -p /heads \
 && chown 10001:10001 /heads
USER 10001
# The three built-in checkpoint names are re-pointed at the baked local copy (nothing is fetched).
ENV HF_HUB_OFFLINE=1 \
    HF_HOME=/tmp/hf \
    LAYA_HOST=0.0.0.0 LAYA_PORT=8000 LAYA_PRELOAD=1 LAYA_MAX_LOADED=1 LAYA_DEVICE=cpu \
    LAYA_MODELS=english LAYA_DEFAULT_MODEL=english \
    LAYA_EXTRA_MODELS='{"english": "/opt/laya-weights", "multilingual": "/opt/laya-weights/multilingual", "typed-decisions": "/opt/laya-weights/typed-decisions"}'
EXPOSE 8000
CMD ["python", "/router/shim.py"]
