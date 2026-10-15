FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 RAGRA_HOME=/data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY ragra ./ragra
RUN pip install .
CMD ["python", "-m", "ragra.cli", "serve-hosted"]
