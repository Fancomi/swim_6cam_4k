#pragma once

#include <source_location>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace swim::test {

using Function = void (*)();

struct Case {
  std::string_view name;
  Function function;
};

inline std::vector<Case>& registry() {
  static std::vector<Case> registered_cases;
  return registered_cases;
}

struct Register {
  Register(std::string_view name, Function function) {
    registry().push_back({name, function});
  }
};

[[noreturn]] inline void fail(
    std::string_view expression,
    std::source_location at = std::source_location::current()) {
  throw std::runtime_error(std::string(at.file_name()) + ":" +
                           std::to_string(at.line()) + ": " +
                           std::string(expression));
}

template <class FunctionType>
void check_throws_with(FunctionType&& function, std::string_view expected) {
  try {
    std::forward<FunctionType>(function)();
  } catch (const std::exception& error) {
    if (std::string_view{error.what()} == expected) {
      return;
    }
    fail("exception message mismatch");
  }
  fail("expected exception was not thrown");
}

}  // namespace swim::test

#define SWIM_JOIN2(a, b) a##b
#define SWIM_JOIN(a, b) SWIM_JOIN2(a, b)
#define TEST_CASE(name)                                                       \
  static void name();                                                        \
  static ::swim::test::Register SWIM_JOIN(register_, __LINE__){#name, &name}; \
  static void name()
#define CHECK(expr)                            \
  do {                                         \
    if (!(expr)) {                             \
      ::swim::test::fail(#expr);               \
    }                                          \
  } while (false)
#define CHECK_EQ(a, b) CHECK((a) == (b))
#define CHECK_THROWS_WITH(expr, message) \
  ::swim::test::check_throws_with([&] { static_cast<void>(expr); }, message)
