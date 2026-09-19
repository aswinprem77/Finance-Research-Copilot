FROM python:3.12-slim
WORKDIR /app

# CPU-only torch. sentence-transformers pulls torch in, and the default wheel
# carries CUDA libraries this image will never use - several GB of them.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the retrieval models into the image. Without this the first live poll
# downloads weights from Hugging Face into a container-local cache that dies
# with the container - a slow, network-dependent surprise on every run.
ENV HF_HOME=/app/.cache/huggingface
ENV SENTENCE_TRANSFORMERS_HOME=/app/.cache/huggingface
RUN python -c "\
from sentence_transformers import CrossEncoder, SentenceTransformer; \
SentenceTransformer('BAAI/bge-small-en-v1.5'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

COPY src ./src
COPY config ./config
COPY benchmarks ./benchmarks
COPY tests/fixtures ./tests/fixtures
RUN useradd --create-home copilot \
    && mkdir -p /app/data \
    && chown copilot:copilot /app/data \
    && chown -R copilot:copilot /app/.cache
USER copilot
ENTRYPOINT ["python", "-m", "src.pipeline"]
CMD ["--demo"]
