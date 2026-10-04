# CodeInsight を、コンテナで動かすためのイメージ（解析・ビューアー・AI解説）。
# 対象のリポジトリは、読み取り専用で /work/target に割り当てる（Makefile の docker-* を使う）。
# AIは、ホスト（macOS など）で動く Ollama を使う: http://host.docker.internal:11434/v1（コンテナには Ollama を含めない）。
# Goのアダプターは、Goのツールチェーンをイメージに含めないため使えない（Goのファイルは解析失敗として記録される）。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CODEINSIGHT_IN_CONTAINER=1 \
    CODEINSIGHT_DATA_DIR=/data \
    CODEINSIGHT_AI_BASE_URL=http://host.docker.internal:11434/v1

# git は、変更履歴（history・tests など）の解析に使う
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/codeinsight
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

# 非特権の利用者で動かす。/data（解析結果のDB）は、名前付きボリュームを割り当てる
RUN useradd --uid 1000 --create-home codeinsight \
    && mkdir -p /data /work \
    && chown codeinsight /data
USER codeinsight
WORKDIR /work
VOLUME ["/data"]
EXPOSE 8765

# 対象はコンテナの中で git の「所有者が違う」警告を避けるため、読み取りだけで済む設定にする
RUN git config --global --add safe.directory '*'

ENTRYPOINT ["codeinsight"]
CMD ["--help"]
