#pragma once

#include <swim/core/frame.hpp>

#include <cstdint>
#include <memory>

struct GLFWwindow;

namespace swim::cudagl {

inline constexpr std::uint32_t kCudaGlFrameBackendTag = 0x43474C31U;   // CGL1
inline constexpr std::uint32_t kCudaGlDecodedSurfaceTag = 0x43474431U; // CGD1

// Shared per-process objects for the CUDA/GL backend. GLFW owns the single GL
// context (created on the main thread, made current on the render thread). The
// CUDA context is the primary context of the same adapter GLFW picked, so
// CUDA-GL interop registration is valid.
struct CudaGlContext final {
  GLFWwindow* gl_context = nullptr;      // hidden/base context, GL resource owner
  int cuda_device = 0;
  bool glfw_owned = false;

  CudaGlContext() = default;
  ~CudaGlContext();
  CudaGlContext(const CudaGlContext&) = delete;
  CudaGlContext& operator=(const CudaGlContext&) = delete;
};

// A decoded NV12 frame living in CUDA device memory. The backend tag lets the
// renderer recover the CUDA pointers; the owning FrameLease keeps the AVFrame
// (and therefore the CUdeviceptr) alive until the GPU upload completes.
struct CudaGlDecodedFrame final {
  // CUdeviceptr for the two NV12 planes and their pitch (bytes per row).
  unsigned long long luma_ptr = 0;
  unsigned long long chroma_ptr = 0;
  std::size_t luma_pitch = 0;
  std::size_t chroma_pitch = 0;
  std::uint32_t width = 0;
  std::uint32_t height = 0;
  swim::core::FrameMetadata metadata;
  // Opaque owner (AVFrame holder); released when the lease refcount hits zero.
  void* owner = nullptr;
};

}  // namespace swim::cudagl
