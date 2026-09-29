#include "util.h"

#define MAX_RETRY 3

static int call_count = 0;

int add(int a, int b) {
    call_count++;
    return a + b;
}

int factorial(int n) {
    if (n <= 1) {
        return 1;
    }
    return n * factorial(n - 1);
}
