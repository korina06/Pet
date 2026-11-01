# 桌宠的容器镜像（第 195 步 / 评价15 T2-7）。
#
# 定位先说清：这是**能力展示 / Linux 上跑一遍**用的，不是"把桌宠装进容器给用户用"。
# 桌宠的体验有一半在"双击就能开、窗口浮在桌面上"，那部分容器给不了；
# 但后端这半边（FastAPI + SQLite + 工具链）在容器里能完整跑起来，CI/演示都有用。
#
# 版本策略：基础镜像钉到 **python:3.13-slim**（3.13 是小版本号，随补丁更新），
# 依赖版本在 requirements.txt 里全钉死（含 pytest，见评价11 P2-2）。
# 没有钉镜像 digest —— 那需要在推送时联网查 registry，我这边查不到，
# 写一个猜的 digest 比不写更糟（看起来钉了、其实是假的）。
FROM python:3.13-slim

# 容器里不要写 .pyc、日志立刻刷出来（不然 docker logs 看不到实时输出）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先只拷依赖清单：改代码不会让 pip 那层缓存失效
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷源码。.dockerignore 已经把 .env / data / 评价*.md 之类挡在外面
COPY . .

# 数据目录挂出来：SQLite 库、日志、备份都在这里，容器重建不该丢
VOLUME ["/app/data"]

EXPOSE 8000

# 健康检查直接打自己的 /api/health（第 192 步的 Host 白名单放行 127.0.0.1/localhost）
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"]

# 绑 127.0.0.1 与第 157 步的取舍一致；容器里要对外必须 -p 映射 + 改 --host 0.0.0.0，
# 那时**必须**同时把 ALLOWED_HOSTS 配成你实际访问用的域名/IP，否则会被 Host 白名单 403。
CMD ["python", "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000"]
