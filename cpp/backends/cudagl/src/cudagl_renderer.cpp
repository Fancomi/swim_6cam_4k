#include <swim/cudagl/cudagl_renderer.hpp>

#include <swim/core/asset_format.hpp>
#include <swim/core/render_completion_gate.hpp>

#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

// cudaGL.h includes <GL/gl.h>, whose declarations depend on Windows types
// (WINGDIAPI/APIENTRY). Pull in windows.h first so those macros are defined.
#include <windows.h>
#include <GL/gl.h>

#include <cuda.h>
#include <cuda_runtime.h>
#include <cudaGL.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

namespace swim::cudagl {
namespace {

constexpr std::array<const char*, 6> kCameraOrder{
    "cam3", "cam2", "cam1", "cam4", "cam5", "cam6"};
constexpr float kPerimeterTolerance = 1.0F / 64.0F;
constexpr float kInclusiveExpansion = 1.0F / 16.0F;

// GLSL 330 port of stitch.metal. Additive FP16 accumulation then normalize.
const char* kVertexSrc = R"GLSL(#version 330 core
layout(location=0) in vec2 output_position;
layout(location=1) in vec2 uv_in;
uniform vec2 u_output_size;
uniform vec2 u_position_offset;
uniform vec2 u_mesh_min;
uniform vec2 u_mesh_max;
uniform float u_perimeter_tolerance;
uniform float u_inclusive_expansion;
uniform uint u_expand_perimeter;
out vec2 v_uv;
void main(){
  vec2 raster = output_position;
  if (u_expand_perimeter != 0u){
    if (abs(output_position.x-u_mesh_min.x)<=u_perimeter_tolerance) raster.x-=u_inclusive_expansion;
    else if (abs(output_position.x-u_mesh_max.x)<=u_perimeter_tolerance) raster.x+=u_inclusive_expansion;
    if (abs(output_position.y-u_mesh_min.y)<=u_perimeter_tolerance) raster.y-=u_inclusive_expansion;
    else if (abs(output_position.y-u_mesh_max.y)<=u_perimeter_tolerance) raster.y+=u_inclusive_expansion;
  }
  vec2 pixel = raster + u_position_offset;
  gl_Position = vec4(pixel.x/u_output_size.x*2.0-1.0, 1.0-pixel.y/u_output_size.y*2.0, 0.0, 1.0);
  v_uv = uv_in;
}
)GLSL";

const char* kFragmentNv12Src = R"GLSL(#version 330 core
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_luma;    // R8, [0,1]
uniform sampler2D u_chroma;  // RG8, [0,1]
uniform sampler2D u_weight;  // R, feather
uniform vec2 u_texel;
uniform vec2 u_weight_origin;
uniform vec2 u_weight_size;
uniform uint u_color_matrix;
uniform uint u_full_range;
float weight_at(){
  vec2 wuv = (gl_FragCoord.xy - u_weight_origin) / u_weight_size;
  return texture(u_weight, wuv).r;
}
vec3 ycbcr_to_rgb(float y, vec2 cbcr){
  float luma, cb, cr;
  if (u_full_range != 0u){
    luma = y; cb = (cbcr.x-128.0/255.0)*(255.0/254.0); cr = (cbcr.y-128.0/255.0)*(255.0/254.0);
  } else {
    luma = (y-16.0/255.0)*(255.0/219.0); cb = (cbcr.x-128.0/255.0)*(255.0/224.0); cr = (cbcr.y-128.0/255.0)*(255.0/224.0);
  }
  if (u_color_matrix==1u) return vec3(luma+1.4020*cr, luma-0.344136*cb-0.714136*cr, luma+1.7720*cb);
  if (u_color_matrix==2u) return vec3(luma+1.4746*cr, luma-0.164553*cb-0.571353*cr, luma+1.8814*cb);
  return vec3(luma+1.5748*cr, luma-0.187324*cb-0.468124*cr, luma+1.8556*cb);
}
void main(){
  float w = weight_at();
  vec2 base = vec2(v_uv.x, 1.0-v_uv.y);
  float y = texture(u_luma, base + 0.5*u_texel).r;
  vec2 cbcr = texture(u_chroma, base + u_texel).rg;
  vec3 rgb = ycbcr_to_rgb(y, cbcr);
  frag = vec4(rgb*w, w);
}
)GLSL";

const char* kResolveVertexSrc = R"GLSL(#version 330 core
out vec2 v_uv;
void main(){
  vec2 p = vec2((gl_VertexID<<1)&2, gl_VertexID&2);
  v_uv = p;
  gl_Position = vec4(p*2.0-1.0, 0.0, 1.0);
}
)GLSL";

const char* kResolveFragmentSrc = R"GLSL(#version 330 core
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_accum;
uniform ivec4 u_logical;  // xy = logical content size (rest unused)
void main(){
  ivec2 pix = ivec2(gl_FragCoord.xy);
  if (pix.x >= u_logical.x || pix.y >= u_logical.y){ frag = vec4(0,0,0,1); return; }
  vec4 v = texelFetch(u_accum, pix, 0);
  vec3 rgb = v.a > 0.0 ? clamp(v.rgb / v.a, 0.0, 1.0) : vec3(0.0);
  frag = vec4(rgb, 1.0);
}
)GLSL";

void cuda_check(CUresult r, const char* op) {
  if (r != CUDA_SUCCESS) {
    const char* name = nullptr;
    cuGetErrorName(r, &name);
    throw std::runtime_error(std::string(op) + " failed: " +
                             (name ? name : "unknown"));
  }
}
void cuda_check(cudaError_t r, const char* op) {
  if (r != cudaSuccess) {
    throw std::runtime_error(std::string(op) + " failed: " +
                             cudaGetErrorString(r));
  }
}

std::uint64_t steady_ns() noexcept {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::steady_clock::now().time_since_epoch())
          .count());
}

}  // namespace

namespace {

GLuint compile_shader(GLenum type, const char* src) {
  GLuint shader = glCreateShader(type);
  glShaderSource(shader, 1, &src, nullptr);
  glCompileShader(shader);
  GLint ok = 0;
  glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
  if (ok == 0) {
    GLint len = 0;
    glGetShaderiv(shader, GL_INFO_LOG_LENGTH, &len);
    std::string log(static_cast<std::size_t>(len > 0 ? len : 1), '\0');
    glGetShaderInfoLog(shader, len, nullptr, log.data());
    glDeleteShader(shader);
    throw std::runtime_error("GL shader compile failed: " + log);
  }
  return shader;
}

GLuint link_program(GLuint vs, GLuint fs) {
  GLuint program = glCreateProgram();
  glAttachShader(program, vs);
  glAttachShader(program, fs);
  glLinkProgram(program);
  GLint ok = 0;
  glGetProgramiv(program, GL_LINK_STATUS, &ok);
  if (ok == 0) {
    GLint len = 0;
    glGetProgramiv(program, GL_INFO_LOG_LENGTH, &len);
    std::string log(static_cast<std::size_t>(len > 0 ? len : 1), '\0');
    glGetProgramInfoLog(program, len, nullptr, log.data());
    glDeleteProgram(program);
    throw std::runtime_error("GL program link failed: " + log);
  }
  return program;
}

// A GL texture registered with CUDA for device-to-device NV12 upload.
struct CudaGlTexture final {
  GLuint texture = 0;
  CUgraphicsResource resource = nullptr;
  std::uint32_t width = 0;
  std::uint32_t height = 0;
  GLenum internal_format = 0;
  GLenum format = 0;
};

}  // namespace

class CudaGlStitchRenderer::Impl {
 public:
  struct CameraResources final {
    GLuint vao = 0;
    GLuint vbo = 0;
    GLuint ebo = 0;
    GLuint weight_tex = 0;
    GLsizei index_count = 0;
    float weight_x = 0, weight_y = 0, weight_w = 0, weight_h = 0;
    float mesh_min_x = 0, mesh_min_y = 0, mesh_max_x = 0, mesh_max_y = 0;
    // Per-lane luma (R8) + chroma (RG8) upload textures, CUDA-registered.
    CudaGlTexture luma;
    CudaGlTexture chroma;
  };

  struct OutputSlot final {
    GLuint texture = 0;
    std::atomic_bool busy{false};
  };

  Impl(std::shared_ptr<CudaGlContext> context,
       const swim::core::RuntimeAsset& asset,
       const swim::core::AppConfig& config,
       swim::core::RuntimeCounters* metrics, CudaGlCompletedOutputSink sink)
      : context_(std::move(context)),
        logical_width_(asset.logical_width),
        logical_height_(asset.logical_height),
        encoded_width_(asset.encoded_width),
        encoded_height_(asset.encoded_height),
        output_capacity_(config.output_pool == 0 ? 4 : config.output_pool),
        publication_(metrics),
        sink_(std::move(sink)) {
    if (asset.cameras.size() != cameras_.size()) {
      throw std::invalid_argument("CUDA/GL renderer requires six cameras");
    }
    for (std::size_t i = 0; i < cameras_.size(); ++i) {
      if (asset.cameras[i].camera_id != kCameraOrder[i]) {
        throw std::invalid_argument(
            "CUDA/GL renderer camera order must be cam3,cam2,cam1,cam4,cam5,cam6");
      }
    }
    cuda_check(cuInit(0), "cuInit");
    // GL objects and the CUDA context are created lazily on the first submit(),
    // which runs on the render thread that owns the GL context. The constructor
    // runs on the main thread where the context is not current.
    publication_.publish([this](auto& m) noexcept {
      m.render_inflight_capacity.store(1, std::memory_order_relaxed);
      m.render_output_capacity.store(output_capacity_,
                                     std::memory_order_relaxed);
    });
    asset_ = &asset;
  }

  ~Impl() { destroy(); }

  void ensure_initialized() {
    if (initialized_) {
      return;
    }
    // submit() runs on the render thread; make the shared GL context current
    // here (once) and load the entry points before any GL call.
    make_context_current();
    load_gl_functions();
    // Driver-API interop (cuGraphicsGLRegisterImage, cuMemcpy2D) needs a current
    // CUDA context on this thread. The device primary context shares UVA with
    // FFmpeg's decode context, so decoded CUdeviceptrs are addressable here.
    cuda_check(cuInit(0), "cuInit");
    CUdevice dev = 0;
    cuda_check(cuDeviceGet(&dev, context_->cuda_device), "cuDeviceGet");
    cuda_check(cuDevicePrimaryCtxRetain(&cuda_context_, dev),
               "cuDevicePrimaryCtxRetain");
    cuda_check(cuCtxSetCurrent(cuda_context_), "cuCtxSetCurrent");
    build_programs();
    build_output_targets();
    upload_camera_resources(*asset_);
    asset_ = nullptr;
    initialized_ = true;
  }

  bool submit(const swim::core::RenderSnapshot& snapshot) noexcept {
    if (fatal_.load(std::memory_order_acquire)) {
      return false;
    }
    try {
      ensure_initialized();
      // Gather the six decoded CUDA frames.
      std::array<CudaGlDecodedFrame*, 6> frames{};
      for (std::size_t i = 0; i < 6; ++i) {
        const auto& lease = snapshot.frames[i];
        if (!lease || lease.metadata().camera_index != i) {
          return false;
        }
        if (lease.backend_tag() != kCudaGlDecodedSurfaceTag) {
          return false;
        }
        auto* holder =
            static_cast<CudaGlDecodedFrame*>(lease.native(kCudaGlDecodedSurfaceTag));
        if (holder == nullptr) {
          return false;
        }
        frames[i] = holder;
      }

      OutputSlot* slot = acquire_output();
      if (slot == nullptr) {
        publication_.publish([](auto& m) noexcept {
          m.pool_exhaustion.fetch_add(1, std::memory_order_relaxed);
          m.render_output_pool_misses.fetch_add(1, std::memory_order_relaxed);
        });
        return false;
      }

      record_first_submit();
      upload_and_stitch(frames, slot);
      glFinish();
      record_completion();

      const GLuint out_tex = slot->texture;
      if (sink_) {
        sink_(out_tex);
      }
      // Preview owns presentation; the slot is immediately reusable because the
      // present path samples the texture synchronously under the GL context.
      slot->busy.store(false, std::memory_order_release);
      return true;
    } catch (const std::exception& e) {
      record_fatal(e.what());
      return false;
    } catch (...) {
      record_fatal("unknown CUDA/GL submission failure");
      return false;
    }
  }

  void drain() {
    flush_metrics();
    if (has_fatal_error()) {
      throw std::runtime_error(fatal_error_message());
    }
  }

  bool has_fatal_error() const noexcept {
    return fatal_.load(std::memory_order_acquire);
  }
  std::string fatal_error_message() const {
    std::lock_guard lock(fatal_mutex_);
    return fatal_message_;
  }

 private:
  void record_fatal(std::string msg) noexcept {
    try {
      std::lock_guard lock(fatal_mutex_);
      if (fatal_message_.empty()) {
        fatal_message_ = std::move(msg);
      }
    } catch (...) {
    }
    fatal_.store(true, std::memory_order_release);
  }

  void record_first_submit() noexcept {
    auto expected = std::uint64_t{0};
    const auto now = steady_ns();
    if (first_submit_ns_.compare_exchange_strong(expected, now,
                                                 std::memory_order_relaxed,
                                                 std::memory_order_relaxed)) {
      publication_.publish([now](auto& m) noexcept {
        auto zero = std::uint64_t{0};
        m.render_first_submit_ns.compare_exchange_strong(
            zero, now, std::memory_order_relaxed, std::memory_order_relaxed);
      });
    }
  }

  void record_completion() noexcept {
    const auto now = steady_ns();
    last_completion_ns_.store(now, std::memory_order_relaxed);
    publication_.publish([now](auto& m) noexcept {
      m.render_completions.fetch_add(1, std::memory_order_relaxed);
      swim::core::record_atomic_max(m.render_last_completion_ns, now);
    });
  }

  void flush_metrics() noexcept {
    publication_.finalize([this](auto& m) noexcept {
      auto zero = std::uint64_t{0};
      m.render_first_submit_ns.compare_exchange_strong(
          zero, first_submit_ns_.load(std::memory_order_relaxed),
          std::memory_order_relaxed, std::memory_order_relaxed);
      swim::core::record_atomic_max(
          m.render_last_completion_ns,
          last_completion_ns_.load(std::memory_order_relaxed));
    });
  }

  OutputSlot* acquire_output() noexcept {
    for (std::uint32_t i = 0; i < output_capacity_; ++i) {
      auto& slot = output_slots_[i];
      bool expected = false;
      if (slot.busy.compare_exchange_strong(expected, true,
                                            std::memory_order_acquire,
                                            std::memory_order_relaxed)) {
        return &slot;
      }
    }
    return nullptr;
  }

  void make_context_current() {
    glfwMakeContextCurrent(context_->gl_context);
  }

  void build_programs() {
    GLuint vs = compile_shader(GL_VERTEX_SHADER, kVertexSrc);
    GLuint fs = compile_shader(GL_FRAGMENT_SHADER, kFragmentNv12Src);
    stitch_program_ = link_program(vs, fs);
    glDeleteShader(vs);
    glDeleteShader(fs);
    GLuint rvs = compile_shader(GL_VERTEX_SHADER, kResolveVertexSrc);
    GLuint rfs = compile_shader(GL_FRAGMENT_SHADER, kResolveFragmentSrc);
    resolve_program_ = link_program(rvs, rfs);
    glDeleteShader(rvs);
    glDeleteShader(rfs);
    glGenVertexArrays(1, &empty_vao_);
  }

  void build_output_targets() {
    // FP16 accumulation target + framebuffer.
    glGenFramebuffers(1, &accum_fbo_);
    glGenTextures(1, &accum_tex_);
    glBindTexture(GL_TEXTURE_2D, accum_tex_);
    glTexImage2D(GL_TEXTURE_2D, 0, static_cast<GLint>(GL_RGBA16F),
                 static_cast<GLsizei>(encoded_width_),
                 static_cast<GLsizei>(encoded_height_), 0, GL_RGBA, GL_FLOAT,
                 nullptr);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);

    // Output RGBA8 slots + their framebuffer (reused for the resolve pass).
    glGenFramebuffers(1, &output_fbo_);
    output_slots_ = std::make_unique<OutputSlot[]>(output_capacity_);
    for (std::uint32_t i = 0; i < output_capacity_; ++i) {
      glGenTextures(1, &output_slots_[i].texture);
      glBindTexture(GL_TEXTURE_2D, output_slots_[i].texture);
      glTexImage2D(GL_TEXTURE_2D, 0, static_cast<GLint>(GL_RGBA8),
                   static_cast<GLsizei>(encoded_width_),
                   static_cast<GLsizei>(encoded_height_), 0, GL_RGBA,
                   GL_UNSIGNED_BYTE, nullptr);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    }
  }

  void make_cuda_gl_texture(CudaGlTexture& tex, std::uint32_t width,
                            std::uint32_t height, GLenum internal_format,
                            GLenum format) {
    tex.width = width;
    tex.height = height;
    tex.internal_format = internal_format;
    tex.format = format;
    glGenTextures(1, &tex.texture);
    glBindTexture(GL_TEXTURE_2D, tex.texture);
    glTexImage2D(GL_TEXTURE_2D, 0, static_cast<GLint>(internal_format),
                 static_cast<GLsizei>(width), static_cast<GLsizei>(height), 0,
                 format, GL_UNSIGNED_BYTE, nullptr);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_MIRRORED_REPEAT);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_MIRRORED_REPEAT);
    cuda_check(cuGraphicsGLRegisterImage(
                   &tex.resource, tex.texture, GL_TEXTURE_2D,
                   CU_GRAPHICS_REGISTER_FLAGS_WRITE_DISCARD),
               "cuGraphicsGLRegisterImage");
  }

  void upload_camera_resources(const swim::core::RuntimeAsset& asset) {
    for (std::size_t i = 0; i < cameras_.size(); ++i) {
      const auto& src = asset.cameras[i];
      auto& cam = cameras_[i];
      if (src.vertices.empty() || src.indices.empty() ||
          src.weight_width == 0 || src.weight_height == 0) {
        throw std::invalid_argument("CUDA/GL camera asset malformed");
      }
      float mnx = src.vertices.front().output_x, mxx = mnx;
      float mny = src.vertices.front().output_y, mxy = mny;
      for (const auto& v : src.vertices) {
        mnx = std::min(mnx, v.output_x); mxx = std::max(mxx, v.output_x);
        mny = std::min(mny, v.output_y); mxy = std::max(mxy, v.output_y);
      }
      cam.mesh_min_x = mnx; cam.mesh_min_y = mny;
      cam.mesh_max_x = mxx; cam.mesh_max_y = mxy;
      cam.weight_x = static_cast<float>(src.weight_x);
      cam.weight_y = static_cast<float>(src.weight_y);
      cam.weight_w = static_cast<float>(src.weight_width);
      cam.weight_h = static_cast<float>(src.weight_height);
      cam.index_count = static_cast<GLsizei>(src.indices.size());

      glGenVertexArrays(1, &cam.vao);
      glBindVertexArray(cam.vao);
      glGenBuffers(1, &cam.vbo);
      glBindBuffer(GL_ARRAY_BUFFER, cam.vbo);
      glBufferData(GL_ARRAY_BUFFER,
                   static_cast<GLsizeiptr>(src.vertices.size() *
                                           sizeof(swim::core::disk::VertexV1)),
                   src.vertices.data(), GL_STATIC_DRAW);
      glGenBuffers(1, &cam.ebo);
      glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, cam.ebo);
      glBufferData(GL_ELEMENT_ARRAY_BUFFER,
                   static_cast<GLsizeiptr>(src.indices.size() *
                                           sizeof(std::uint32_t)),
                   src.indices.data(), GL_STATIC_DRAW);
      // VertexV1 = {output_x, output_y, u, v}; attr0 = pos (offset 0),
      // attr1 = uv (offset 8), stride 16.
      glEnableVertexAttribArray(0);
      glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE,
                            sizeof(swim::core::disk::VertexV1),
                            reinterpret_cast<void*>(0));
      glEnableVertexAttribArray(1);
      glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE,
                            sizeof(swim::core::disk::VertexV1),
                            reinterpret_cast<void*>(8));
      glBindVertexArray(0);

      // Feather weights: 16-bit unorm single channel. Set tight row alignment
      // — width*2 bytes is not a multiple of the default 4-byte unpack stride,
      // so GL would otherwise read past the source buffer.
      glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
      glGenTextures(1, &cam.weight_tex);
      glBindTexture(GL_TEXTURE_2D, cam.weight_tex);
      glTexImage2D(GL_TEXTURE_2D, 0, static_cast<GLint>(GL_R16),
                   static_cast<GLsizei>(src.weight_width),
                   static_cast<GLsizei>(src.weight_height), 0, GL_RED,
                   GL_UNSIGNED_SHORT, src.weights.data());
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);

      make_cuda_gl_texture(cam.luma, kFrameW, kFrameH, GL_R8, GL_RED);
      make_cuda_gl_texture(cam.chroma, kFrameW / 2, kFrameH / 2, GL_RG8, GL_RG);
    }
  }

  void upload_nv12(CameraResources& cam, const CudaGlDecodedFrame& frame) {
    // Map both GL textures for CUDA write, memcpy2D each plane, unmap.
    CUgraphicsResource resources[2] = {cam.luma.resource, cam.chroma.resource};
    cuda_check(cuGraphicsMapResources(2, resources, 0),
               "cuGraphicsMapResources");
    struct Unmap {
      CUgraphicsResource* r;
      ~Unmap() { cuGraphicsUnmapResources(2, r, 0); }
    } unmap{resources};

    CUarray luma_array = nullptr;
    CUarray chroma_array = nullptr;
    cuda_check(cuGraphicsSubResourceGetMappedArray(&luma_array,
                                                   cam.luma.resource, 0, 0),
               "get luma array");
    cuda_check(cuGraphicsSubResourceGetMappedArray(&chroma_array,
                                                   cam.chroma.resource, 0, 0),
               "get chroma array");

    // Copy only the valid frame region; the decoder pitch may exceed width and
    // the GL texture is exactly frame-sized.
    const std::size_t luma_rows = std::min<std::size_t>(cam.luma.height, frame.height);
    CUDA_MEMCPY2D luma{};
    luma.srcMemoryType = CU_MEMORYTYPE_DEVICE;
    luma.srcDevice = static_cast<CUdeviceptr>(frame.luma_ptr);
    luma.srcPitch = frame.luma_pitch;
    luma.dstMemoryType = CU_MEMORYTYPE_ARRAY;
    luma.dstArray = luma_array;
    luma.WidthInBytes = std::min<std::size_t>(cam.luma.width, frame.width);
    luma.Height = luma_rows;
    cuda_check(cuMemcpy2D(&luma), "cuMemcpy2D luma");

    const std::size_t chroma_rows =
        std::min<std::size_t>(cam.chroma.height, frame.height / 2);
    CUDA_MEMCPY2D chroma{};
    chroma.srcMemoryType = CU_MEMORYTYPE_DEVICE;
    chroma.srcDevice = static_cast<CUdeviceptr>(frame.chroma_ptr);
    chroma.srcPitch = frame.chroma_pitch;
    chroma.dstMemoryType = CU_MEMORYTYPE_ARRAY;
    chroma.dstArray = chroma_array;
    chroma.WidthInBytes =
        std::min<std::size_t>(cam.chroma.width, frame.width / 2) * 2;
    chroma.Height = chroma_rows;
    cuda_check(cuMemcpy2D(&chroma), "cuMemcpy2D chroma");
  }

  void upload_and_stitch(const std::array<CudaGlDecodedFrame*, 6>& frames,
                         OutputSlot* slot) {
    // Accumulation pass with additive blend into the FP16 target.
    glBindFramebuffer(GL_FRAMEBUFFER, accum_fbo_);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D,
                           accum_tex_, 0);
    glViewport(0, 0, static_cast<GLsizei>(encoded_width_),
               static_cast<GLsizei>(encoded_height_));
    glClearColor(0, 0, 0, 0);
    glClear(GL_COLOR_BUFFER_BIT);
    glDisable(GL_CULL_FACE);
    glEnable(GL_BLEND);
    glBlendFunc(GL_ONE, GL_ONE);
    glBlendEquation(GL_FUNC_ADD);
    glUseProgram(stitch_program_);

    for (std::size_t i = 0; i < cameras_.size(); ++i) {
      auto& cam = cameras_[i];
      upload_nv12(cam, *frames[i]);

      set_uniform2f("u_output_size", static_cast<float>(encoded_width_),
                    static_cast<float>(encoded_height_));
      set_uniform2f("u_position_offset", 0.5F, 0.5F);
      set_uniform2f("u_mesh_min", cam.mesh_min_x, cam.mesh_min_y);
      set_uniform2f("u_mesh_max", cam.mesh_max_x, cam.mesh_max_y);
      set_uniform1f("u_perimeter_tolerance", kPerimeterTolerance);
      set_uniform1f("u_inclusive_expansion", kInclusiveExpansion);
      set_uniform1ui("u_expand_perimeter", 1U);
      set_uniform2f("u_texel", 1.0F / static_cast<float>(frames[i]->width),
                    1.0F / static_cast<float>(frames[i]->height));
      set_uniform2f("u_weight_origin", cam.weight_x, cam.weight_y);
      set_uniform2f("u_weight_size", cam.weight_w, cam.weight_h);
      set_uniform1ui("u_color_matrix",
                     static_cast<unsigned>(frames[i]->metadata.color_matrix));
      set_uniform1ui("u_full_range",
                     frames[i]->metadata.pixel_format ==
                             swim::core::PixelFormat::nv12_full_range
                         ? 1U
                         : 0U);
      set_sampler("u_luma", 0);
      set_sampler("u_chroma", 1);
      set_sampler("u_weight", 2);
      glActiveTexture(GL_TEXTURE0);
      glBindTexture(GL_TEXTURE_2D, cam.luma.texture);
      glActiveTexture(GL_TEXTURE0 + 1);
      glBindTexture(GL_TEXTURE_2D, cam.chroma.texture);
      glActiveTexture(GL_TEXTURE0 + 2);
      glBindTexture(GL_TEXTURE_2D, cam.weight_tex);

      glBindVertexArray(cam.vao);
      glDrawElements(GL_TRIANGLES, cam.index_count, GL_UNSIGNED_INT, nullptr);
    }
    glBindVertexArray(0);

    // Resolve pass: normalize into the RGBA8 output slot.
    glDisable(GL_BLEND);
    glBindFramebuffer(GL_FRAMEBUFFER, output_fbo_);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D,
                           slot->texture, 0);
    glViewport(0, 0, static_cast<GLsizei>(encoded_width_),
               static_cast<GLsizei>(encoded_height_));
    glUseProgram(resolve_program_);
    set_sampler_prog(resolve_program_, "u_accum", 0);
    set_ivec2(resolve_program_, "u_logical",
              static_cast<int>(logical_width_),
              static_cast<int>(logical_height_));
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, accum_tex_);
    glBindVertexArray(empty_vao_);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    glBindVertexArray(0);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
  }

  void set_uniform2f(const char* name, float a, float b) {
    glUniform2f(glGetUniformLocation(stitch_program_, name), a, b);
  }
  void set_uniform1f(const char* name, float a) {
    glUniform1f(glGetUniformLocation(stitch_program_, name), a);
  }
  void set_uniform1ui(const char* name, unsigned a) {
    glUniform1ui(glGetUniformLocation(stitch_program_, name), a);
  }
  void set_sampler(const char* name, int unit) {
    glUniform1i(glGetUniformLocation(stitch_program_, name), unit);
  }
  void set_sampler_prog(GLuint prog, const char* name, int unit) {
    glUniform1i(glGetUniformLocation(prog, name), unit);
  }
  void set_ivec2(GLuint prog, const char* name, int a, int b) {
    glUniform4i(glGetUniformLocation(prog, name), a, b, 0, 0);
  }

  void destroy() noexcept {
    flush_metrics();
    for (auto& cam : cameras_) {
      if (cam.luma.resource) cuGraphicsUnregisterResource(cam.luma.resource);
      if (cam.chroma.resource) cuGraphicsUnregisterResource(cam.chroma.resource);
    }
    if (cuda_context_ != nullptr) {
      CUdevice dev = 0;
      if (cuDeviceGet(&dev, context_->cuda_device) == CUDA_SUCCESS) {
        cuDevicePrimaryCtxRelease(dev);
      }
      cuda_context_ = nullptr;
    }
  }

  static constexpr std::uint32_t kFrameW = 3840;
  static constexpr std::uint32_t kFrameH = 2160;

  std::shared_ptr<CudaGlContext> context_;
  const swim::core::RuntimeAsset* asset_ = nullptr;
  bool initialized_ = false;
  std::uint32_t logical_width_, logical_height_, encoded_width_, encoded_height_;
  std::uint32_t output_capacity_;
  std::array<CameraResources, 6> cameras_;
  GLuint stitch_program_ = 0;
  GLuint resolve_program_ = 0;
  GLuint empty_vao_ = 0;
  GLuint accum_fbo_ = 0;
  GLuint accum_tex_ = 0;
  GLuint output_fbo_ = 0;
  std::unique_ptr<OutputSlot[]> output_slots_;
  CUcontext cuda_context_ = nullptr;
  swim::core::RuntimeCounterPublication publication_;
  CudaGlCompletedOutputSink sink_;
  std::atomic_uint64_t first_submit_ns_{0};
  std::atomic_uint64_t last_completion_ns_{0};
  std::atomic_bool fatal_{false};
  mutable std::mutex fatal_mutex_;
  std::string fatal_message_;
};

CudaGlStitchRenderer::CudaGlStitchRenderer(
    std::shared_ptr<CudaGlContext> context,
    const swim::core::RuntimeAsset& asset, const swim::core::AppConfig& config,
    swim::core::RuntimeCounters* metrics, CudaGlCompletedOutputSink sink)
    : impl_(std::make_unique<Impl>(std::move(context), asset, config, metrics,
                                   std::move(sink))) {}

CudaGlStitchRenderer::~CudaGlStitchRenderer() = default;

bool CudaGlStitchRenderer::submit(
    const swim::core::RenderSnapshot& snapshot) noexcept {
  return impl_->submit(snapshot);
}
void CudaGlStitchRenderer::drain() { impl_->drain(); }
bool CudaGlStitchRenderer::has_fatal_error() const noexcept {
  return impl_->has_fatal_error();
}
std::string CudaGlStitchRenderer::fatal_error_message() const {
  return impl_->fatal_error_message();
}

}  // namespace swim::cudagl


