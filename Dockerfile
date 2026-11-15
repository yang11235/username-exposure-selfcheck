FROM python:3.11-slim AS base
LABEL maintainer="Soxoj <soxoj@protonmail.com>"
WORKDIR /app
RUN pip install --no-cache-dir --upgrade pip
RUN apt-get update && \
    apt-get install --no-install-recommends -y \
      build-essential \
      python3-dev \
      pkg-config \
      libcairo2-dev \
      libxml2-dev \
      libxslt1-dev \
    && rm -rf /var/lib/apt/lists/* /tmp/*
COPY . .
RUN YARL_NO_EXTENSIONS=1 python3 -m pip install --no-cache-dir .

# 容器内必须绑 0.0.0.0，否则 -p 发布过来的流量到不了：docker 的 DNAT 打到
# 容器 eth0，绑 127.0.0.1 只会听容器自己的 loopback，外部一律 connection
# refused。镜像的职责是"能被访问到"，安全边界在宿主机那一侧。
#
# 注意：本项目的 Web 端**没有鉴权、没有限流**，/history 是所有人共用的一
# 份查询记录。所以：
#   - 本机用：docker run -d -p 127.0.0.1:5000:5000 ...  ← 绑定写回环地址
#     这一侧的限定同样有效，且不牺牲容器可达性。
#   - 不要直接 `-p 5000:5000` 在公网主机上跑。
# 具体见 docs/deploy.md 的容器一节。
ENV FLASK_HOST=0.0.0.0

# Web UI variant: auto-launches the web interface on $PORT
FROM base AS web
RUN pip install --no-cache-dir '.[pdf]'
ENV PORT=5000
EXPOSE 5000
ENTRYPOINT ["sh", "-c", "exec maigret --web \"$PORT\""]

# Default variant (last stage = `docker build .` target): CLI, backwards-compatible
FROM base AS cli
ENTRYPOINT ["maigret"]
