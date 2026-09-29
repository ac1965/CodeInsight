#ifndef UTIL_H
#define UTIL_H

typedef struct Point {
    int x;
    int y;
} Point;

int add(int a, int b);

#ifdef ENABLE_DEBUG_LOG
void debug_log(const char *message);
#endif

#endif
