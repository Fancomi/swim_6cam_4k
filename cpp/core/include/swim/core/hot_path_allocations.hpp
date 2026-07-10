#pragma once

#include <cstdint>

namespace swim::core {

class HotPathAllocationScope final {
 public:
  HotPathAllocationScope() noexcept;
  HotPathAllocationScope(const HotPathAllocationScope&) = delete;
  HotPathAllocationScope& operator=(const HotPathAllocationScope&) = delete;
  HotPathAllocationScope(HotPathAllocationScope&&) = delete;
  HotPathAllocationScope& operator=(HotPathAllocationScope&&) = delete;
  ~HotPathAllocationScope();
};

std::uint64_t hot_path_allocation_count() noexcept;

}  // namespace swim::core
