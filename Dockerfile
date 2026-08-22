FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
# editable 安装：保持 main.py 的 __file__ 指向 /app/src/，
# 使 ui/main.py 的 _PROJECT_ROOT = parents[3] 正确解析到 /app（data/ 挂载点）。
RUN pip install --no-cache-dir -e .

COPY app.py .streamlit .env.example ./
COPY pages ./pages
COPY scripts ./scripts
COPY data/samples ./data/samples
COPY data/cache ./data/cache

EXPOSE 8501

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
