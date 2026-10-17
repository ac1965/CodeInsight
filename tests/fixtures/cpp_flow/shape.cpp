#include <stdexcept>
#include <vector>
#include <string>

int g_total = 0;

namespace geo {

class Shape {
public:
    Shape(int w, int h) : w_(w), h_(h) { count_++; }
    virtual ~Shape() { count_--; }
    int area() const { return w_ * h_; }
    void scale(int k) {
        if (k <= 0) throw std::invalid_argument("k");
        w_ *= k;
        h_ *= k;
        g_total += k;
    }
    static int count() { return count_; }
private:
    int w_, h_;
    static int count_;
};

int Shape::count_ = 0;

int sum_areas(const std::vector<Shape *> &shapes) {
    int total = 0;
    for (auto *s : shapes) {
        total += s->area();
    }
    try {
        shapes.at(100);
    } catch (const std::out_of_range &e) {
        total = -1;
    } catch (...) {
        throw;
    }
    return total;
}

}  // namespace geo

namespace errs {

void thrower() { throw std::runtime_error("x"); }

void guarded() {
    try {
        thrower();
    } catch (const std::exception &) {
    }
}

void local_catch() {
    try {
        throw std::runtime_error("y");
    } catch (const std::runtime_error &) {
        g_total = 1;
    }
}

void middle() { thrower(); }
void top() { middle(); }

}  // namespace errs
