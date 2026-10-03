#ifndef OPS_H
#define OPS_H

typedef int (*binary_op)(int, int);

struct Calculator {
    binary_op op;
    int total;
};

#define SQUARE(x) ((x) * (x))

int add(int a, int b);
int mul(int a, int b);
int apply(struct Calculator *calc, int value);
int fib(int n);

#ifdef USE_DEBUG
void trace(const char *msg);
#endif

#endif
