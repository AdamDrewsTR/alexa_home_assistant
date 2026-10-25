FROM python:3.13-slim
WORKDIR /app
COPY connector.py ha_config.py ./
RUN pip install --no-cache-dir "aiohttp>=3.11,<4" "ruamel.yaml>=0.18,<0.19"
ENV PYTHONUNBUFFERED=1
CMD ["python", "connector.py"]
