#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int g_counter = 0;
static int s_cache[4];

struct Config {
    int limit;
    char *name;
};

static int check(int v) {
    return v > 0;
}

int process(struct Config *cfg, const char *input, int mode) {
    int total = 0;
    int i;
    char *buf = malloc(64);
    if (cfg == NULL || input == NULL)
        return -1;
    strcpy(buf, input);
    for (i = 0; i < cfg->limit; i++) {
        if (check(i) && mode > 0) {
            total += i;
            g_counter++;
        } else if (mode < 0) {
            continue;
        } else {
            break;
        }
    }
    switch (mode) {
    case 0:
        total = 1;
    case 1:
        total += 2;
        break;
    default:
        goto fail;
    }
    cfg->limit = total;
    s_cache[0] = total;
    printf(input);
    free(buf);
    return total;
fail:
    free(buf);
    exit(2);
}

int run_cmd(const char *cmd) {
    return system(cmd);
}

const char *home(void) {
    return getenv("HOME");
}

int factorial(int n) {
    int acc = 1;
    while (n > 1) {
        acc = acc * n;
        n--;
    }
    do {
        acc++;
    } while (acc < 0);
    return n <= 1 ? acc : factorial(n - 1);
}

#define IS_SPACE(c) ((c) == ' ' || (c) == '\t')
#define IS_UPPER(c) ((c) >= 'A' && (c) <= 'Z')

int classify(int c) {
    if (IS_SPACE(c) || c == '\n' || c == '\r')
        return 1;
    return IS_UPPER(c) ? 2 : 3;
}
