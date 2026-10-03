#include "ops.h"

int add(int a, int b) {
    return a + b;
}

int mul(int a, int b) {
    return a * b;
}

int apply(struct Calculator *calc, int value) {
    calc->total = calc->op(calc->total, value);
    return calc->total;
}

int fib(int n) {
    if (n < 2) {
        return n;
    }
    return fib(n - 1) + fib(n - 2);
}

#ifdef USE_DEBUG
void trace(const char *msg) {
    (void)msg;
}
#endif
