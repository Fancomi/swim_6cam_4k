#include "test_support.hpp"

#include <algorithm>
#include <exception>
#include <iostream>

int main() {
  unsigned int failures = 0;
  for (const auto& test_case : swim::test::registry()) {
    try {
      test_case.function();
      std::cout << "PASS " << test_case.name << '\n';
    } catch (const std::exception& error) {
      ++failures;
      std::cerr << "FAIL " << test_case.name << ": " << error.what() << '\n';
    } catch (...) {
      ++failures;
      std::cerr << "FAIL " << test_case.name << ": unknown exception\n";
    }
  }
  return static_cast<int>(std::min(failures, 255U));
}
