#include <swim/d3d11/d3d11_frame.hpp>

#include <exception>
#include <stdexcept>
#include <utility>

namespace swim::d3d11 {

D3D11OutputLease::D3D11OutputLease(D3D11OutputSlot* slot) noexcept
    : slot_(slot) {}

D3D11OutputLease::D3D11OutputLease(const D3D11OutputLease& other) noexcept
    : slot_(other.slot_), lifetime_anchor_(other.lifetime_anchor_) {
  if (slot_ != nullptr) {
    slot_->references.fetch_add(1, std::memory_order_relaxed);
  }
}

D3D11OutputLease& D3D11OutputLease::operator=(
    const D3D11OutputLease& other) noexcept {
  if (this == &other) {
    return *this;
  }
  auto* next = other.slot_;
  if (next != nullptr) {
    next->references.fetch_add(1, std::memory_order_relaxed);
  }
  reset();
  slot_ = next;
  lifetime_anchor_ = other.lifetime_anchor_;
  return *this;
}

D3D11OutputLease::D3D11OutputLease(D3D11OutputLease&& other) noexcept
    : slot_(std::exchange(other.slot_, nullptr)),
      lifetime_anchor_(std::move(other.lifetime_anchor_)) {}

D3D11OutputLease& D3D11OutputLease::operator=(
    D3D11OutputLease&& other) noexcept {
  if (this == &other) {
    return *this;
  }
  reset();
  slot_ = std::exchange(other.slot_, nullptr);
  lifetime_anchor_ = std::move(other.lifetime_anchor_);
  return *this;
}

D3D11OutputLease::~D3D11OutputLease() { reset(); }

ID3D11Texture2D* D3D11OutputLease::texture() const noexcept {
  return slot_ == nullptr ? nullptr : slot_->texture.Get();
}

ID3D11RenderTargetView* D3D11OutputLease::rtv() const noexcept {
  return slot_ == nullptr ? nullptr : slot_->rtv.Get();
}

ID3D11ShaderResourceView* D3D11OutputLease::srv() const noexcept {
  return slot_ == nullptr ? nullptr : slot_->srv.Get();
}

void D3D11OutputLease::anchor_lifetime(std::shared_ptr<void> owner) noexcept {
  if (lifetime_anchor_ == nullptr) {
    lifetime_anchor_ = std::move(owner);
  }
}

void D3D11OutputLease::reset() noexcept {
  if (slot_ != nullptr) {
    auto* slot = std::exchange(slot_, nullptr);
    slot->owner->release(slot);
  }
  lifetime_anchor_.reset();
}

D3D11OutputPool::D3D11OutputPool(std::shared_ptr<D3D11Context> context,
                                 std::uint32_t capacity, std::uint32_t width,
                                 std::uint32_t height)
    : context_(std::move(context)),
      capacity_(capacity),
      slots_(capacity == 0 ? nullptr
                           : std::make_unique<D3D11OutputSlot[]>(capacity)) {
  if (context_ == nullptr || context_->device == nullptr) {
    throw std::invalid_argument("D3D11 output pool requires a valid context");
  }
  if (capacity_ == 0 || capacity_ > 64) {
    throw std::invalid_argument(
        "D3D11 output pool capacity must be between 1 and 64");
  }
  if (width == 0 || height == 0) {
    throw std::invalid_argument("D3D11 output dimensions must be nonzero");
  }

  D3D11_TEXTURE2D_DESC desc{};
  desc.Width = width;
  desc.Height = height;
  desc.MipLevels = 1;
  desc.ArraySize = 1;
  desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
  desc.SampleDesc.Count = 1;
  desc.Usage = D3D11_USAGE_DEFAULT;
  desc.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;

  for (std::uint32_t index = 0; index < capacity_; ++index) {
    auto& slot = slots_[index];
    slot.pool_index = index;
    slot.owner = this;
    if (FAILED(context_->device->CreateTexture2D(&desc, nullptr,
                                                 slot.texture.GetAddressOf()))) {
      throw std::runtime_error("cannot allocate D3D11 output texture");
    }
    if (FAILED(context_->device->CreateRenderTargetView(
            slot.texture.Get(), nullptr, slot.rtv.GetAddressOf()))) {
      throw std::runtime_error("cannot create D3D11 output render target view");
    }
    if (FAILED(context_->device->CreateShaderResourceView(
            slot.texture.Get(), nullptr, slot.srv.GetAddressOf()))) {
      throw std::runtime_error(
          "cannot create D3D11 output shader resource view");
    }
  }
}

D3D11OutputPool::~D3D11OutputPool() noexcept {
  for (std::uint32_t index = 0; index < capacity_; ++index) {
    if (slots_[index].references.load(std::memory_order_acquire) != 0) {
      std::terminate();
    }
  }
}

std::optional<D3D11OutputLease> D3D11OutputPool::try_acquire() noexcept {
  for (std::uint32_t index = 0; index < capacity_; ++index) {
    auto& slot = slots_[index];
    std::uint32_t expected = 0;
    if (!slot.references.compare_exchange_strong(expected, 1,
                                                 std::memory_order_acquire,
                                                 std::memory_order_relaxed)) {
      continue;
    }
    const auto usage = in_use_.fetch_add(1, std::memory_order_relaxed) + 1;
    auto previous_high = high_water_.load(std::memory_order_relaxed);
    while (usage > previous_high &&
           !high_water_.compare_exchange_weak(previous_high, usage,
                                              std::memory_order_relaxed,
                                              std::memory_order_relaxed)) {
    }
    return std::optional<D3D11OutputLease>{D3D11OutputLease{&slot}};
  }
  return std::nullopt;
}

std::uint32_t D3D11OutputPool::in_use() const noexcept {
  return in_use_.load(std::memory_order_relaxed);
}

std::uint32_t D3D11OutputPool::high_water() const noexcept {
  return high_water_.load(std::memory_order_relaxed);
}

void D3D11OutputPool::release(D3D11OutputSlot* slot) noexcept {
  const auto previous =
      slot->references.fetch_sub(1, std::memory_order_release);
  if (previous == 0) {
    std::terminate();
  }
  if (previous == 1) {
    in_use_.fetch_sub(1, std::memory_order_relaxed);
  }
}

}  // namespace swim::d3d11
