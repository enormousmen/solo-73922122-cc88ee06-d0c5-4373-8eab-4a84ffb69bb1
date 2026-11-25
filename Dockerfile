FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DATA_DIR=/data \
    TILE_DELAY=0.15

VOLUME /data
EXPOSE 8000

# 单 worker：调度状态在内存中，多进程会破坏"同时最多一个渲染子进程"的约束
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
