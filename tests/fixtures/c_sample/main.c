#include <stdio.h>
#include "util.h"

int global_counter = 0;

int main(void) {
    Point origin = {0, 0};
    global_counter = add(origin.x, origin.y);
    printf("%d\n", global_counter);
    return 0;
}
