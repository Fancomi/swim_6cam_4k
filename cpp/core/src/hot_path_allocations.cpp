#include <swim/core/hot_path_allocations.hpp>

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <limits>
#include <new>

#if defined(_WIN32)
#include <malloc.h>
#endif

namespace {

// Aligned allocation is spelled differently per toolchain: C11 std::aligned_alloc
// on POSIX libc, _aligned_malloc/_aligned_free on the MSVC CRT (which does not
// provide std::aligned_alloc). Aligned blocks must be released by the matching
// free, so both the allocate and the aligned delete operators route here.
void* platform_aligned_alloc(std::size_t alignment, std::size_t size) {
#if defined(_WIN32)
  return _aligned_malloc(size, alignment);
#else
  return std::aligned_alloc(alignment, size);
#endif
}

void platform_aligned_free(void* pointer) noexcept {
#if defined(_WIN32)
  _aligned_free(pointer);
#else
  std::free(pointer);
#endif
}

std::atomic_uint64_t hot_path_allocations{0};
thread_local std::uint32_t hot_path_scope_depth = 0;

void record_hot_path_allocation() noexcept {
  if (hot_path_scope_depth != 0) {
    hot_path_allocations.fetch_add(1, std::memory_order_relaxed);
  }
}

void* allocate_unaligned(std::size_t requested_size) {
  const auto size = requested_size == 0 ? std::size_t{1} : requested_size;
  for (;;) {
    if (auto* allocation = std::malloc(size); allocation != nullptr) {
      return allocation;
    }
    const auto handler = std::get_new_handler();
    if (handler == nullptr) {
      throw std::bad_alloc{};
    }
    handler();
  }
}

void* allocate_aligned(std::size_t requested_size,
                       std::align_val_t requested_alignment) {
  const auto alignment = static_cast<std::size_t>(requested_alignment);
  if (alignment < alignof(void*) || (alignment & (alignment - 1)) != 0) {
    throw std::bad_alloc{};
  }

  auto size = requested_size == 0 ? std::size_t{1} : requested_size;
  const auto remainder = size % alignment;
  if (remainder != 0) {
    const auto padding = alignment - remainder;
    if (size > std::numeric_limits<std::size_t>::max() - padding) {
      throw std::bad_alloc{};
    }
    size += padding;
  }

  for (;;) {
    if (auto* allocation = platform_aligned_alloc(alignment, size);
        allocation != nullptr) {
      return allocation;
    }
    const auto handler = std::get_new_handler();
    if (handler == nullptr) {
      throw std::bad_alloc{};
    }
    handler();
  }
}

}  // namespace

namespace swim::core {

HotPathAllocationScope::HotPathAllocationScope() noexcept {
  ++hot_path_scope_depth;
}

HotPathAllocationScope::~HotPathAllocationScope() {
  --hot_path_scope_depth;
}

std::uint64_t hot_path_allocation_count() noexcept {
  return hot_path_allocations.load(std::memory_order_relaxed);
}

}  // namespace swim::core

void* operator new(std::size_t size) {
  auto* allocation = allocate_unaligned(size);
  record_hot_path_allocation();
  return allocation;
}

void* operator new[](std::size_t size) {
  auto* allocation = allocate_unaligned(size);
  record_hot_path_allocation();
  return allocation;
}

void* operator new(std::size_t size, std::align_val_t alignment) {
  auto* allocation = allocate_aligned(size, alignment);
  record_hot_path_allocation();
  return allocation;
}

void* operator new[](std::size_t size, std::align_val_t alignment) {
  auto* allocation = allocate_aligned(size, alignment);
  record_hot_path_allocation();
  return allocation;
}

void* operator new(std::size_t size, const std::nothrow_t&) noexcept {
  try {
    return ::operator new(size);
  } catch (...) {
    return nullptr;
  }
}

void* operator new[](std::size_t size, const std::nothrow_t&) noexcept {
  try {
    return ::operator new[](size);
  } catch (...) {
    return nullptr;
  }
}

void* operator new(std::size_t size, std::align_val_t alignment,
                   const std::nothrow_t&) noexcept {
  try {
    return ::operator new(size, alignment);
  } catch (...) {
    return nullptr;
  }
}

void* operator new[](std::size_t size, std::align_val_t alignment,
                     const std::nothrow_t&) noexcept {
  try {
    return ::operator new[](size, alignment);
  } catch (...) {
    return nullptr;
  }
}

void operator delete(void* pointer) noexcept { std::free(pointer); }

void operator delete[](void* pointer) noexcept { std::free(pointer); }

void operator delete(void* pointer, std::size_t) noexcept {
  std::free(pointer);
}

void operator delete[](void* pointer, std::size_t) noexcept {
  std::free(pointer);
}

void operator delete(void* pointer, std::align_val_t) noexcept {
  platform_aligned_free(pointer);
}

void operator delete[](void* pointer, std::align_val_t) noexcept {
  platform_aligned_free(pointer);
}

void operator delete(void* pointer, std::size_t, std::align_val_t) noexcept {
  platform_aligned_free(pointer);
}

void operator delete[](void* pointer, std::size_t,
                       std::align_val_t) noexcept {
  platform_aligned_free(pointer);
}

void operator delete(void* pointer, const std::nothrow_t&) noexcept {
  std::free(pointer);
}

void operator delete[](void* pointer, const std::nothrow_t&) noexcept {
  std::free(pointer);
}

void operator delete(void* pointer, std::align_val_t,
                     const std::nothrow_t&) noexcept {
  platform_aligned_free(pointer);
}

void operator delete[](void* pointer, std::align_val_t,
                       const std::nothrow_t&) noexcept {
  platform_aligned_free(pointer);
}
