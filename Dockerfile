# ---------------------------------------------------------------------------
# DataMate submission image.
# 1. Replace BASE_IMAGE with the image named in the organizers' Docker guideline
#    (it must provide CUDA >= 12.8 and a Blackwell-compatible PyTorch/vLLM).
# 2. Put model weights under ./models/ (they are baked into the image: no
#    network is available at run time). Declare every model in README.md.
# Build:  docker build --build-arg BASE_IMAGE=<image> -t datamate .
# Run:    docker run --rm --gpus all --network none \
#           -v $PWD/data:/data:ro -v $PWD/result:/result datamate
# ---------------------------------------------------------------------------
ARG BASE_IMAGE=REPLACE_WITH_IMAGE_FROM_ORGANIZER_GUIDELINE
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    VLLM_NO_USAGE_STATS=1 \
    DO_NOT_TRACK=1 \
    DATAMATE_MODEL=/models/main

WORKDIR /app
COPY requirements.txt .
# vLLM / torch come from the base image; only light deps are installed here.
RUN pip install --no-cache-dir -r requirements.txt

COPY models/ /models/
COPY src/ src/
COPY scripts/ scripts/
COPY run.py conventions.json ./

ENTRYPOINT ["bash", "scripts/launch_dp.sh"]
