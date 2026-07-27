FROM python:3.11-slim

WORKDIR /app

# 安装依赖（利用 Docker 缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# Render 通过 PORT 环境变量指定端口
ENV PORT=8000
EXPOSE 8000

# 启动命令
CMD ["python", "main.py"]
