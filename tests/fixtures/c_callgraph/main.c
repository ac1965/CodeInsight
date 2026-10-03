#include <stdio.h>
#include "ops.h"

static int helper(int x) {
    return SQUARE(x);
}

int main(void) {
    struct Calculator calc = {add, 0};
    binary_op op = mul;
    int r = op(2, 3);
    apply(&calc, helper(r));
    printf("%d\n", fib(5));
    return 0;
}
