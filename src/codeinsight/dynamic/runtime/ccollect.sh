#!/bin/sh
# C の収集器（gcov）。コンテナの中で動く。標準的な POSIX sh だけを使う。
#
# 対象は読み取り専用で割り当てられているため、作業領域（tmpfs）へ複製し、その中でビルドと実行を行う（対象は変更しない）。
# `cc`・`gcc`・`clang`・`c99` の呼び出しに `--coverage -O0 -g` を足す wrapper を PATH の先頭に置く（再コンパイルを伴う）。
# 実行後、*.gcda から gcov のテキスト（関数の実行回数・行の実行回数）を作り、出力先へ書き出す。引数・戻り値などの値は記録しない。
#
#   使い方: ccollect.sh --root <対象> --out <出力先のディレクトリ> -- <コマンド> [引数...]
set -u

ROOT="" ; OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    --) shift; break ;;
    *) echo "ccollect: 不明なオプション: $1" >&2; exit 64 ;;
  esac
done
[ -n "$ROOT" ] && [ -n "$OUT" ] && [ $# -gt 0 ] || { echo "ccollect: --root・--out・コマンドが必要です" >&2; exit 64; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/ci-ccollect.XXXXXX") || exit 70
mkdir -p "$WORK/bin" "$WORK/src" "$OUT" || exit 70
cp -R "$ROOT/." "$WORK/src/" || { echo "ccollect: 対象の複製に失敗しました" >&2; exit 70; }

for tool in cc gcc clang c99; do
  real=$(command -v "$tool" 2>/dev/null) || continue
  case "$real" in "$WORK"/*) continue ;; esac
  printf '#!/bin/sh\nexec "%s" "$@" --coverage -O0 -g\n' "$real" > "$WORK/bin/$tool"
  chmod +x "$WORK/bin/$tool"
done

( cd "$WORK/src" && PATH="$WORK/bin:$PATH" CC=cc "$@" )
code=$?
echo "$code" > "$OUT/exit_code"

# *.gcda ごとに、そのディレクトリで gcov を実行する（*.gcov を作る）
find "$WORK/src" -name '*.gcda' | while IFS= read -r gcda; do
  ( cd "$(dirname "$gcda")" && gcov -b -o . "$(basename "$gcda")" >/dev/null 2>&1 )
done

{
  printf '==WORK %s\n' "$WORK/src"
  find "$WORK/src" -name '*.gcov' | while IFS= read -r file; do
    printf '==GCOV %s\n' "$(dirname "$file")"
    cat "$file"
  done
} > "$OUT/gcov.txt"
exit 0
