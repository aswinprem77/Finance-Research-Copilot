FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY config ./config
COPY benchmarks ./benchmarks
COPY tests/fixtures ./tests/fixtures
RUN useradd --create-home copilot && mkdir -p /app/data && chown copilot:copilot /app/data
USER copilot
ENTRYPOINT ["python", "-m", "src.pipeline"]
CMD ["--demo"]
