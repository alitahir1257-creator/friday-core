FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LAYA_HOST=0.0.0.0 \
    LAYA_PORT=8000 \
    LAYA_DEVICE=cpu \
    LAYA_PRELOAD=1 \
    LAYA_MODELS=english \
    LAYA_MAX_LOADED=1 \
    LAYA_THREADS=2

ARG TORCH_VERSION=2.14.0
RUN python -m pip install --no-cache-dir "torch==${TORCH_VERSION}" --index-url https://download.pytorch.org/whl/cpu \
    && python -m pip install --no-cache-dir "laya[serve]==0.3.26"

EXPOSE 8000

CMD ["laya-serve"]
