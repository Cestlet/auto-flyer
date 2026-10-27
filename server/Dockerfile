# flyer-image 构建文件：预装 websockets + flask + docker CLI（国内镜像源）
# docker CLI 用于 container_mgr 动态创建/管理每用户的 NapCat 容器（经 docker.sock 连宿主 daemon）
FROM python:3.12-slim
WORKDIR /app

# 1) apt 源切阿里云（python:3.12-slim = Debian 12，deb822 格式）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
      /etc/apt/sources.list.d/debian.sources

# 2) docker CLI（仅客户端，无需 daemon；阿里云 docker-ce 仓库）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/debian/gpg \
       | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] \
https://mirrors.aliyun.com/docker-ce/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# 3) Python 依赖（阿里云 pip 镜像）
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
    'websockets>=12.0' 'flask>=3.0' 'requests>=2.31.0'

COPY . .
