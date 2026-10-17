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

# 呼び出しの辺・入出力の観測（-finstrument-functions と、薄い実行時の層 ci_shim.c）。必要な道具（ar・addr2line）が無ければ、gcov だけにする
INSTRUMENT=0
SHIM_SRC="$(cd "$(dirname "$0")" && pwd)/ci_shim.c"
real_cc=$(command -v cc 2>/dev/null || command -v gcc 2>/dev/null || command -v clang 2>/dev/null)
if [ -f "$SHIM_SRC" ] && [ -n "$real_cc" ] && command -v ar >/dev/null 2>&1 && command -v addr2line >/dev/null 2>&1 && [ -r /proc/self/exe ]; then
  if "$real_cc" -c -O1 -fno-pie -fno-stack-protector "$SHIM_SRC" -o "$WORK/ci_shim.o" 2>/dev/null && ar rcs "$WORK/libcishim.a" "$WORK/ci_shim.o" 2>/dev/null; then
    INSTRUMENT=1
  fi
fi

for tool in cc gcc clang c99; do
  real=$(command -v "$tool" 2>/dev/null) || continue
  case "$real" in "$WORK"/*) continue ;; esac
  if [ "$INSTRUMENT" = 1 ]; then
    printf '#!/bin/sh\nlink=1\nfor a in "$@"; do case "$a" in -c|-E|-S|-M|-MM) link=0 ;; esac; done\nif [ "$link" = 1 ]; then exec "%s" "$@" --coverage -O0 -g -finstrument-functions -fno-pie -no-pie -L"%s" -lcishim -ldl; else exec "%s" "$@" --coverage -O0 -g -finstrument-functions -fno-pie; fi\n' "$real" "$WORK" "$real" > "$WORK/bin/$tool"
  else
    printf '#!/bin/sh\nexec "%s" "$@" --coverage -O0 -g\n' "$real" > "$WORK/bin/$tool"
  fi
  chmod +x "$WORK/bin/$tool"
done

export CI_TRACE="$WORK/trace" CI_WORK="$WORK/src"
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

# 呼び出しの辺・入出力: 実行ごとの記録（trace.<pid>）を、addr2line で関数名・位置に直して、まとめる（値は記録しない）
if [ "$INSTRUMENT" = 1 ]; then
  : > "$WORK/joined.txt"
  for trace in "$WORK"/trace.*; do
    [ -f "$trace" ] || continue
    bin=$(awk -F'\t' '$1=="B"{print $2; exit}' "$trace")
    [ -n "$bin" ] && [ -f "$bin" ] || continue
    awk -F'\t' '$1=="E"{print "0x"$2; print "0x"$3} $1=="I"{print "0x"$3}' "$trace" | sort -u > "$trace.addrs"
    addr2line -f -e "$bin" < "$trace.addrs" > "$trace.sym" 2>/dev/null || continue
    awk -F'\t' -v symfile="$trace.sym" -v addrfile="$trace.addrs" '
      BEGIN { n = 0; while ((getline a < addrfile) > 0) { n++; addr[n] = a; getline fn < symfile; getline loc < symfile; name[a] = fn; where[a] = loc } }
      $1 == "E" { c = "0x" $2; s = "0x" $3; print "E\t" $4 "\t" name[c] "\t" where[c] "\t" name[s] "\t" where[s] }
      $1 == "I" { s = "0x" $3; print "I\t" $5 "\t" $2 "\t" name[s] "\t" where[s] "\t" $4 }
      $1 == "D" { print "D\t" $2 "\t" $3 }
    ' "$trace" >> "$WORK/joined.txt"
  done
  {
    echo "==CALLS"
    awk -F'\t' '$1=="E" && $3!="??" && $5!="??" { key = $3 "\t" $4 "\t" $5 "\t" $6; sum[key] += $2 } END { for (k in sum) print sum[k] "\t" k }' "$WORK/joined.txt" | sort
    echo "==IO"
    awk -F'\t' '$1=="I" && $4!="??" { key = $3 "\t" $4 "\t" $5 "\t" $6; sum[key] += $2 } END { for (k in sum) print sum[k] "\t" k }' "$WORK/joined.txt" | sort
    awk -F'\t' '$1=="D" { e += $2; i += $3 } END { print "==DROPPED\t" e + 0 "\t" i + 0 }' "$WORK/joined.txt"
  } >> "$OUT/gcov.txt"
fi
exit 0
