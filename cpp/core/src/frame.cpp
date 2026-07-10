#include <swim/core/frame.hpp>

#include <stdexcept>
#include <utility>

namespace swim::core {

FrameLease::FrameLease(void* native, NativeLeaseOps ops,
                       FrameMetadata metadata) noexcept
    : native_(native), ops_(ops), metadata_(metadata) {}

FrameLease::FrameLease(const FrameLease& other)
    : native_(other.native_), ops_(other.ops_), metadata_(other.metadata_) {
  if (native_ != nullptr) {
    ops_.retain(native_);
  }
}

FrameLease& FrameLease::operator=(const FrameLease& other) {
  if (this == &other) {
    return *this;
  }

  auto* new_native = other.native_;
  const auto new_ops = other.ops_;
  const auto new_metadata = other.metadata_;
  if (new_native != nullptr) {
    new_ops.retain(new_native);
  }

  reset();
  native_ = new_native;
  ops_ = new_ops;
  metadata_ = new_metadata;
  return *this;
}

FrameLease::FrameLease(FrameLease&& other) noexcept
    : native_(std::exchange(other.native_, nullptr)),
      ops_(std::exchange(other.ops_, {})),
      metadata_(other.metadata_) {}

FrameLease& FrameLease::operator=(FrameLease&& other) noexcept {
  if (this == &other) {
    return *this;
  }

  reset();
  native_ = std::exchange(other.native_, nullptr);
  ops_ = std::exchange(other.ops_, {});
  metadata_ = other.metadata_;
  return *this;
}

FrameLease::~FrameLease() { reset(); }

FrameLease::operator bool() const noexcept { return native_ != nullptr; }

const FrameMetadata& FrameLease::metadata() const noexcept {
  return metadata_;
}

void* FrameLease::native(std::uint32_t expected_backend_tag) const {
  if (ops_.backend_tag != expected_backend_tag) {
    throw std::runtime_error("frame backend tag mismatch");
  }
  return native_;
}

void FrameLease::reset() noexcept {
  if (native_ != nullptr) {
    ops_.release(native_);
  }
  native_ = nullptr;
  ops_ = {};
}

}  // namespace swim::core
