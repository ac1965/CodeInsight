/* CodeInsight の動的解析（C）の薄い実行時の層。コンテナの中で、対象と一緒にビルドされる（利用者のコードではない）。
 *
 * 記録するのは、関数の呼び出しの辺（呼び出し元・呼び出し先のアドレスと回数）と、入出力の種類（ファイルを開く・ネットワーク・
 * 外部プロセス・環境変数の名前）だけ。引数・戻り値・ファイルのパス・コマンドなどの値は記録しない。実行の終了時に、
 * 環境変数 CI_TRACE のパスに、プロセスIDを付けて書く（_exit・異常終了では書かれない）。
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define NOINST __attribute__((no_instrument_function))
#define EDGE_SLOTS (1u << 16)
#define IO_SLOTS 4096

struct edge { uintptr_t callee, caller; unsigned long count; };
struct io { const char *kind; uintptr_t caller; char detail[48]; unsigned long count; };

static struct edge edges[EDGE_SLOTS];
static struct io ios[IO_SLOTS];
static volatile int lock_flag;
static unsigned long dropped_edges, dropped_io;

NOINST static void lock(void) { while (__sync_lock_test_and_set(&lock_flag, 1)) { } }
NOINST static void unlock(void) { __sync_lock_release(&lock_flag); }

NOINST void __cyg_profile_func_enter(void *fn, void *site) {
    uintptr_t callee = (uintptr_t)fn, caller = (uintptr_t)site;
    uint64_t h = (callee * 1000003u) ^ (caller * 998244353u);
    lock();
    for (unsigned probe = 0; probe < 64; probe++) {
        struct edge *e = &edges[(h + probe) & (EDGE_SLOTS - 1)];
        if (e->count == 0) { e->callee = callee; e->caller = caller; e->count = 1; unlock(); return; }
        if (e->callee == callee && e->caller == caller) { e->count++; unlock(); return; }
    }
    dropped_edges++;
    unlock();
}
NOINST void __cyg_profile_func_exit(void *fn, void *site) { (void)fn; (void)site; }

NOINST static void record_io(const char *kind, uintptr_t caller, const char *detail) {
    lock();
    for (unsigned i = 0; i < IO_SLOTS; i++) {
        struct io *e = &ios[i];
        if (e->count == 0) { e->kind = kind; e->caller = caller; snprintf(e->detail, sizeof e->detail, "%s", detail ? detail : ""); e->count = 1; unlock(); return; }
        if (e->kind == kind && e->caller == caller && strcmp(e->detail, detail ? detail : "") == 0) { e->count++; unlock(); return; }
    }
    dropped_io++;
    unlock();
}

/* パスは記録しない。種類だけ（プロジェクトの複製の中・一時領域・システム・その他）。 */
NOINST static const char *path_class(const char *path) {
    if (!path) return "other";
    const char *work = getenv("CI_WORK");
    if (work && strncmp(path, work, strlen(work)) == 0) return "project";
    if (path[0] != '/') return "project";
    if (strncmp(path, "/tmp/", 5) == 0) return "temp";
    if (strncmp(path, "/etc/", 5) == 0 || strncmp(path, "/usr/", 5) == 0 || strncmp(path, "/lib", 4) == 0 || strncmp(path, "/proc/", 6) == 0 || strncmp(path, "/dev/", 5) == 0) return "system";
    return "other";
}

NOINST FILE *fopen(const char *path, const char *mode) {
    static FILE *(*real)(const char *, const char *);
    if (!real) real = (FILE *(*)(const char *, const char *))dlsym(RTLD_NEXT, "fopen");
    const char *kind = (mode && (strchr(mode, 'w') || strchr(mode, 'a') || strchr(mode, '+'))) ? "file_write" : "file_read";
    record_io(kind, (uintptr_t)__builtin_return_address(0), path_class(path));
    return real(path, mode);
}

NOINST int open(const char *path, int flags, ...) {
    static int (*real)(const char *, int, ...);
    if (!real) real = (int (*)(const char *, int, ...))dlsym(RTLD_NEXT, "open");
    mode_t mode = 0;
    if (flags & (O_CREAT | O_TMPFILE)) { va_list ap; va_start(ap, flags); mode = va_arg(ap, mode_t); va_end(ap); }
    int writing = (flags & (O_WRONLY | O_RDWR | O_CREAT | O_TRUNC | O_APPEND)) != 0;
    record_io(writing ? "file_write" : "file_read", (uintptr_t)__builtin_return_address(0), path_class(path));
    return real(path, flags, mode);
}

NOINST int connect(int fd, const struct sockaddr *address, socklen_t length) {
    static int (*real)(int, const struct sockaddr *, socklen_t);
    if (!real) real = (int (*)(int, const struct sockaddr *, socklen_t))dlsym(RTLD_NEXT, "connect");
    record_io("network", (uintptr_t)__builtin_return_address(0), "connect");
    return real(fd, address, length);
}

NOINST int system(const char *command) {
    static int (*real)(const char *);
    if (!real) real = (int (*)(const char *))dlsym(RTLD_NEXT, "system");
    record_io("process", (uintptr_t)__builtin_return_address(0), "system");
    return real(command);
}

NOINST FILE *popen(const char *command, const char *type) {
    static FILE *(*real)(const char *, const char *);
    if (!real) real = (FILE *(*)(const char *, const char *))dlsym(RTLD_NEXT, "popen");
    record_io("process", (uintptr_t)__builtin_return_address(0), "popen");
    return real(command, type);
}

NOINST int execve(const char *path, char *const argv[], char *const envp[]) {
    static int (*real)(const char *, char *const[], char *const[]);
    if (!real) real = (int (*)(const char *, char *const[], char *const[]))dlsym(RTLD_NEXT, "execve");
    record_io("process", (uintptr_t)__builtin_return_address(0), "exec");
    return real(path, argv, envp);
}

/* 環境変数は、名前を記録する（値は記録しない）。 */
NOINST char *getenv(const char *name) {
    static char *(*real)(const char *);
    if (!real) real = (char *(*)(const char *))dlsym(RTLD_NEXT, "getenv");
    if (name && strncmp(name, "CI_", 3) != 0) record_io("env_read", (uintptr_t)__builtin_return_address(0), name);
    return real(name);
}

NOINST __attribute__((destructor)) static void write_trace(void) {
    static char *(*real_getenv)(const char *);
    if (!real_getenv) real_getenv = (char *(*)(const char *))dlsym(RTLD_NEXT, "getenv");
    const char *base = real_getenv("CI_TRACE");
    if (!base) return;
    char path[1024];
    snprintf(path, sizeof path, "%s.%d", base, (int)getpid());
    FILE *(*real_fopen)(const char *, const char *) = (FILE *(*)(const char *, const char *))dlsym(RTLD_NEXT, "fopen");
    FILE *out = real_fopen(path, "w");
    if (!out) return;
    char exe[1024];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
    if (n > 0) { exe[n] = 0; fprintf(out, "B\t%s\n", exe); }
    for (unsigned i = 0; i < EDGE_SLOTS; i++) if (edges[i].count) fprintf(out, "E\t%lx\t%lx\t%lu\n", (unsigned long)edges[i].callee, (unsigned long)edges[i].caller, edges[i].count);
    for (unsigned i = 0; i < IO_SLOTS; i++) if (ios[i].count) fprintf(out, "I\t%s\t%lx\t%s\t%lu\n", ios[i].kind, (unsigned long)ios[i].caller, ios[i].detail, ios[i].count);
    fprintf(out, "D\t%lu\t%lu\n", dropped_edges, dropped_io);
    fclose(out);
}
