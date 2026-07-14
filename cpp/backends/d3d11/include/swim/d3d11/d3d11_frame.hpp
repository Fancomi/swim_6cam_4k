#pragma once

#include <swim/core/frame.hpp>

#include <d3d11.h>
#include <dxgi1_2.h>
#include <wrl/client.h>

#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>

namespace swim::d3d11 {

using Microsoft::WRL::ComPtr;

// Backend tag distinguishing D3D11 native leases from any other backend's.
inline constexpr std::uint32_t kD3D11FrameBackendTag = 0x44583131U;   // DX11
inline constexpr std::uint32_t kD3D11DecodedSurfaceTag = 0x44584431U;  // DXD1

// Shared device objects. One device/context/factory is created per backend and
// shared by the renderer, the Media Foundation decode lanes, and the preview.
struct D3D11Context final {
  ComPtr<ID3D11Device> device;
  ComPtr<ID3D11DeviceContext> immediate_context;
  ComPtr<IDXGIFactory2> factory;
  // The immediate context is not thread-safe. Decode lanes, the render thread,
  // and the preview all funnel GPU submissions through it, so every use is
  // serialized under this mutex.
  std::mutex context_mutex;
};

// A view onto one camera frame. Either an RGBA SRV (bgra path) or a pair of
// NV12 plane SRVs (luma R8 + chroma R8G8). The referenced texture is kept alive
// by the FrameLease that owns the native surface.
struct D3D11FrameView final {
  ID3D11ShaderResourceView* rgba = nullptr;
  ID3D11ShaderResourceView* luma = nullptr;
  ID3D11ShaderResourceView* chroma = nullptr;
  swim::core::FrameMetadata metadata;
};

class D3D11OutputPool;

// One 5002x2102 BGRA output surface. It is simultaneously a render target
// (resolve pass writes it) and a shader resource (preview samples it).
struct D3D11OutputSlot final {
  ComPtr<ID3D11Texture2D> texture;
  ComPtr<ID3D11RenderTargetView> rtv;
  ComPtr<ID3D11ShaderResourceView> srv;
  std::atomic_uint32_t references{0};
  std::uint32_t pool_index = 0;
  D3D11OutputPool* owner = nullptr;
};

// Reference-counted borrow of an output slot. Mirrors MetalOutputLease: copies
// retain for independent in-flight use; the pool must outlive every lease.
class D3D11OutputLease final {
 public:
  D3D11OutputLease() = default;
  D3D11OutputLease(const D3D11OutputLease&) noexcept;
  D3D11OutputLease& operator=(const D3D11OutputLease&) noexcept;
  D3D11OutputLease(D3D11OutputLease&&) noexcept;
  D3D11OutputLease& operator=(D3D11OutputLease&&) noexcept;
  ~D3D11OutputLease();

  explicit operator bool() const noexcept { return slot_ != nullptr; }
  ID3D11Texture2D* texture() const noexcept;
  ID3D11RenderTargetView* rtv() const noexcept;
  ID3D11ShaderResourceView* srv() const noexcept;
  void anchor_lifetime(std::shared_ptr<void> owner) noexcept;

 private:
  friend class D3D11OutputPool;
  explicit D3D11OutputLease(D3D11OutputSlot* slot) noexcept;
  void reset() noexcept;
  D3D11OutputSlot* slot_{};
  std::shared_ptr<void> lifetime_anchor_;
};

// Fixed pool of output surfaces. Destruction with outstanding references is a
// contract violation and terminates, matching the Metal pool.
class D3D11OutputPool final {
 public:
  D3D11OutputPool(std::shared_ptr<D3D11Context> context, std::uint32_t capacity,
                  std::uint32_t width, std::uint32_t height);
  ~D3D11OutputPool() noexcept;
  D3D11OutputPool(const D3D11OutputPool&) = delete;
  D3D11OutputPool& operator=(const D3D11OutputPool&) = delete;

  std::optional<D3D11OutputLease> try_acquire() noexcept;
  std::uint32_t in_use() const noexcept;
  std::uint32_t high_water() const noexcept;

 private:
  friend class D3D11OutputLease;
  void release(D3D11OutputSlot* slot) noexcept;

  std::shared_ptr<D3D11Context> context_;
  std::uint32_t capacity_{};
  std::unique_ptr<D3D11OutputSlot[]> slots_;
  std::atomic_uint32_t in_use_{0};
  std::atomic_uint32_t high_water_{0};
};

}  // namespace swim::d3d11
