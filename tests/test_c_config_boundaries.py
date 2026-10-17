"""C/C++ の設定値（config）と入口・境界（boundaries）: 環境変数・getopt・設定ファイル・定数マクロ、シグナル・atexit・スレッド・ネットワーク。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeinsight.cli import main

SOURCE = """\
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <pthread.h>
#include <unistd.h>
#include <sys/socket.h>
#include <getopt.h>
#define MAX_RETRIES 5
#define GREETING "hello"
#define SQUARE(x) ((x) * (x))
#define EMPTY_GUARD_H
static struct option long_options[] = {
    {"verbose", no_argument, 0, 'v'},
    {"output", required_argument, 0, 'o'},
};
static void on_int(int sig) { (void)sig; }
static void cleanup(void) { }
static void *worker(void *arg) { return arg; }
int load(void) {
    const char *home = getenv("HOME");
    if (home) { printf("%s\\n", home); }
    setenv("MODE", "fast", 1);
    getenv(NULL ? "X" : "DYNAMIC");
    FILE *f = fopen("/etc/app.conf", "r");
    FILE *g = fopen("data.txt", "r");
    (void)f; (void)g;
    return 0;
}
int parse(int argc, char **argv) {
    int c;
    while ((c = getopt(argc, argv, "hv:o::")) != -1) { }
    if (strcmp(argv[1], "--dry-run") == 0) return 1;
    return 0;
}
int main(int argc, char **argv) {
    signal(SIGINT, on_int);
    atexit(cleanup);
    pthread_t t;
    pthread_create(&t, NULL, worker, NULL);
    int s = socket(2, 1, 0);
    listen(s, 5);
    return parse(argc, argv) + load();
}
"""


@pytest.fixture
def db(tmp_path: Path, capsys) -> str:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.c").write_text(SOURCE, encoding="utf-8")
    path = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", path]) == 0
    capsys.readouterr()
    return path


def config_items(capsys, db: str) -> list[dict]:
    capsys.readouterr()
    assert main(["config", "--db", db, "--format", "json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_environment_variables_with_owner_and_uses(db: str, capsys) -> None:
    items = config_items(capsys, db)
    home = next(i for i in items if i["kind"] == "env" and i["name"] == "HOME")
    assert home["owner"] == "load" and "未設定なら NULL" in home["detail"] and "home" in home["detail"]
    assert [u["line"] for u in home["uses"]] == [22]  # 代入した変数 home の、同じ関数内での使用
    assert {(i["kind"], i["name"]) for i in items if i["kind"] in ("env", "env_write")} >= {("env", "HOME"), ("env_write", "MODE"), ("env", "<動的>")}


def test_command_line_options_from_getopt_long_options_and_argv_comparison(db: str, capsys) -> None:
    options = {i["name"]: i for i in config_items(capsys, db) if i["kind"] == "cli_option"}
    assert {"-h", "-v", "-o", "--verbose", "--output", "--dry-run"} <= set(options)
    assert "引数を取る" in options["-v"]["detail"] and "引数は任意" in options["-o"]["detail"] and "引数なし" in options["-h"]["detail"]
    assert options["--dry-run"]["confidence"] == "inferred" and options["--verbose"]["confidence"] == "inferred"  # 表・比較からの推定


def test_config_files_and_constants(db: str, capsys) -> None:
    items = config_items(capsys, db)
    files = {i["name"]: i for i in items if i["kind"] == "config_file"}
    assert list(files) == ["/etc/app.conf"] and files["/etc/app.conf"]["confidence"] == "inferred"  # data.txt は設定ファイルとみなさない
    constants = {i["name"]: i["default"] for i in items if i["kind"] == "constant"}
    assert constants == {"MAX_RETRIES": "5", "GREETING": '"hello"'}  # 関数形・空のマクロ（ガード）は対象外


def test_boundaries_signals_atexit_threads_and_network_resolve_their_targets(db: str, capsys) -> None:
    capsys.readouterr()
    assert main(["boundaries", "--db", db, "--format", "json"]) == 0
    items = json.loads(capsys.readouterr().out)
    by_label = {i["label"]: i for i in items if i["path"] == "app.c"}
    signal = next(i for label, i in by_label.items() if "signal SIGINT" in label)
    assert signal["kind"] == "event" and signal["target"] == "on_int"
    assert next(i for label, i in by_label.items() if "atexit" in label)["target"] == "cleanup"
    thread = next(i for label, i in by_label.items() if "pthread_create" in label)
    assert thread["kind"] == "thread" and thread["target"] == "worker"
    kinds = {i["kind"] for i in items}
    assert "network" in kinds and "entry" in kinds  # socket・listen と、main


def test_coverage_note_no_longer_says_c_is_unsupported(db: str, capsys) -> None:
    capsys.readouterr()
    assert main(["config", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "のみ対応です" not in out


def test_environment_for_c_reads_build_settings_platform_branches_and_commands(tmp_path: Path, capsys) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "env.c").write_text(
        '#include <stdlib.h>\n#include <unistd.h>\n#ifdef __linux__\nint linux_only(void) { return 1; }\n#endif\n#if defined(_WIN32)\nint win(void) { return 2; }\n#endif\n'
        'int run(void) { system("ls -l /tmp"); execlp("grep", "grep", "x", (char *)0); return 0; }\n',
        encoding="utf-8",
    )
    build = tmp_path / "build"
    build.mkdir()
    (build / "compile_commands.json").write_text(
        json.dumps([{"directory": str(root), "file": str(root / "env.c"), "arguments": ["gcc", "-std=gnu11", "-DHAVE_X=1", "-DDEBUG", "-lm", "-lpthread", "-I/usr/include/foo", "-c", str(root / "env.c")]}]),
        encoding="utf-8",
    )
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", db, "--compile-commands", str(build)]) == 0
    capsys.readouterr()
    assert main(["environment", "--db", db, "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["c_build"]["standards"] == {"gnu11": 1} and set(data["c_build"]["libraries"]) == {"m", "pthread"} and data["c_build"]["compilers"] == {"gcc": 1}
    assert {"HAVE_X=1", "DEBUG"} <= set(data["c_build"]["defines"])
    assert {s["detail"] for s in data["platform_checks"]} == {"#ifdef __linux__", "#if defined(_WIN32)"}  # OS・コンパイラによる分岐
    assert {s["detail"] for s in data["executables"]} == {"system(ls…)", "execlp(grep)"}
