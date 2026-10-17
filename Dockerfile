# CodeInsight を、コンテナで動かすためのイメージ（解析・ビューアー・AI解説・資料生成）。
# 必要な個別ソフトウェアは、すべてこのイメージに入れる:
#   git（変更履歴）・Go（Goアダプターの補助プログラムを作る）・build-essential（Cの標準ヘッダー）・
#   graphviz（図）・Chromium（資料のPDF）・make（make reading）
# このイメージが、既定の実行環境（Makefile は、RUNTIME=docker が既定。`make reading` / `make serve` / `make analyze` など。docker compose も使える）。
# 対象のリポジトリは、読み取り専用で、ホストと同じパスに割り当てる。解析結果のDBは、ホストの ~/.codeinsight を割り当てて、
# ホストとコンテナで共有する。動的解析（dynamic-run）は、Docker を入れ子にしないため、コンテナの中からは実行しない（ホストで実行する）。
# AIは、ホスト（macOS など）で動く Ollama を使う: http://host.docker.internal:11434/v1（コンテナには Ollama を含めない）。

# Go のツールチェーンは、公式イメージから取り出す（版を固定する。コンテナ内ではネットワークを使わずに作る）
FROM golang:1.23-bookworm AS go

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/usr/local/go/bin:$PATH \
    CODEINSIGHT_IN_CONTAINER=1 \
    CODEINSIGHT_DATA_DIR=/data \
    CODEINSIGHT_BROWSER=/usr/local/bin/chromium-container \
    CODEINSIGHT_AI_BASE_URL=http://host.docker.internal:11434/v1 \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=*

RUN apt-get update \
    && apt-get install -y --no-install-recommends git make build-essential graphviz chromium fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY --from=go /usr/local/go /usr/local/go

# コンテナの中の Chromium は、サンドボックスを使えない（ユーザー名前空間が無い）ため、この起動用の包みで --no-sandbox を付ける
RUN printf '#!/bin/sh\nexec chromium --no-sandbox "$@"\n' > /usr/local/bin/chromium-container \
    && chmod +x /usr/local/bin/chromium-container
# `make reading` は `uv run ...` で呼ぶ。コンテナでは、すでにインストール済みなので、`run` を取り除いて実行する包み（UV=ci-uv で指定）
RUN printf '#!/bin/sh\n[ "$1" = run ] && shift\nexec "$@"\n' > /usr/local/bin/ci-uv && chmod +x /usr/local/bin/ci-uv

# 依存は、uv.lock の固定した版を入れる（再現できるように。ビルド時だけネットワークを使う）。そのあとで、本体を入れる
WORKDIR /opt/codeinsight
COPY pyproject.toml uv.lock README.md LICENSE Makefile ./
RUN pip install uv \
    && uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && pip uninstall -y uv \
    && rm -f /tmp/requirements.txt
COPY src ./src
RUN pip install --no-deps .

# /data（解析結果のDB）と /work（出力先）は、ホストの利用者の権限で書けるよう、実行時に --user で指定する
RUN mkdir -p /data /work && chmod 777 /data /work
ENV HOME=/tmp
WORKDIR /work
EXPOSE 8765
LABEL org.opencontainers.image.title="CodeInsight" org.opencontainers.image.description="コードリーディング支援（静的解析・ビューアー・AI解説）"

ENTRYPOINT ["codeinsight"]
CMD ["--help"]
