# sbx-office:lite — 精简版沙箱镜像，结构对齐 AILab 文档 08 的 L0～L6 分层
# 与大集群差异：不装 Chromium / Node 办公库（省内存），其余目录约定一致。
# 必须 buildx 双架构构建（amd64 + arm64），见 tests/t1_image.sh。
FROM python:3.12-slim-trixie

# L1 系统工具 + L5 中文字体
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libreoffice-impress-nogui libreoffice-writer-nogui libreoffice-calc-nogui \
      poppler-utils fonts-noto-cjk fontconfig tini \
 && rm -rf /var/lib/apt/lists/*

# L3 Python 办公库（版本锁定）
RUN pip install --no-cache-dir \
      python-pptx==1.0.2 python-docx==1.2.0 openpyxl==3.1.5 \
      matplotlib==3.10.7 pandas==2.3.3 pypdf==6.1.3

# 字体映射：模型和模板里写“微软雅黑/宋体”，实际落到 Noto CJK
COPY fonts-local.conf /etc/fonts/local.conf
RUN fc-cache -f

# L6 Skills 与工具
COPY skills/ /opt/skills/
COPY smoke/ /opt/ailab-tools/smoke/

# 运行时约定：非 root、/workspace/{inputs,outputs}
RUN useradd -u 10001 -m -d /home/sandbox sandbox \
 && mkdir -p /workspace/inputs /workspace/outputs \
 && chown -R 10001:10001 /workspace
USER 10001
WORKDIR /workspace
ENV HOME=/home/sandbox PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
