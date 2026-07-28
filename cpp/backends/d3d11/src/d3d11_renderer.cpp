#include <swim/d3d11/d3d11_renderer.hpp>

#include <swim/core/asset_format.hpp>
#include <swim/core/render_completion_gate.hpp>

#include <d3dcompiler.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iterator>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>

namespace swim::d3d11 {
namespace {

constexpr std::array<const char*, 6> kCameraOrder{
    "cam3", "cam2", "cam1", "cam4", "cam5", "cam6"};
constexpr float kPerimeterTolerance = 1.0F / 64.0F;
constexpr float kInclusiveExpansion = 1.0F / 16.0F;

struct VertexUniforms final {
  float output_width;
  float output_height;
  float position_offset_x;
  float position_offset_y;
  float mesh_min_x;
  float mesh_min_y;
  float mesh_max_x;
  float mesh_max_y;
  float perimeter_tolerance;
  float inclusive_expansion;
  std::uint32_t expand_perimeter;
  std::uint32_t reserved;
};

struct FragmentUniforms final {
  float texture_texel_x;
  float texture_texel_y;
  float weight_origin_x;
  float weight_origin_y;
  float weight_width;
  float weight_height;
  std::uint32_t color_matrix;
  std::uint32_t full_range;
};

static_assert(sizeof(VertexUniforms) == 48);
static_assert(sizeof(FragmentUniforms) == 32);

std::string read_shader_source() {
#ifndef SWIM_D3D11_SHADER_SOURCE_PATH
#error "SWIM_D3D11_SHADER_SOURCE_PATH must name stitch.hlsl"
#endif
  std::ifstream input(SWIM_D3D11_SHADER_SOURCE_PATH, std::ios::binary);
  if (!input) {
    throw std::runtime_error("cannot open D3D11 shader source: "
                             SWIM_D3D11_SHADER_SOURCE_PATH);
  }
  return {std::istreambuf_iterator<char>{input},
          std::istreambuf_iterator<char>{}};
}

ComPtr<ID3DBlob> compile_entry(const std::string& source, const char* entry,
                               const char* target) {
  ComPtr<ID3DBlob> code;
  ComPtr<ID3DBlob> errors;
  const UINT flags = D3DCOMPILE_ENABLE_STRICTNESS | D3DCOMPILE_OPTIMIZATION_LEVEL3;
  const auto hr = D3DCompile(source.data(), source.size(), "stitch.hlsl",
                             nullptr, nullptr, entry, target, flags, 0,
                             code.GetAddressOf(), errors.GetAddressOf());
  if (FAILED(hr)) {
    std::string message = "cannot compile D3D11 shader entry ";
    message += entry;
    if (errors != nullptr && errors->GetBufferSize() != 0) {
      message += ": ";
      message.append(static_cast<const char*>(errors->GetBufferPointer()),
                     errors->GetBufferSize());
    }
    throw std::runtime_error(message);
  }
  return code;
}

std::uint64_t steady_nanoseconds() noexcept {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::steady_clock::now().time_since_epoch())
          .count());
}

}  // namespace

class D3D11StitchRenderer::Impl
    : public std::enable_shared_from_this<D3D11StitchRenderer::Impl> {
 public:
  struct CameraResources final {
    ComPtr<ID3D11Buffer> vertices;
    ComPtr<ID3D11Buffer> indices;
    ComPtr<ID3D11Texture2D> weight_texture;
    ComPtr<ID3D11ShaderResourceView> weight_srv;
    UINT index_count = 0;
    std::size_t vertex_bytes = 0;
    float weight_x = 0.0F;
    float weight_y = 0.0F;
    float weight_width = 0.0F;
    float weight_height = 0.0F;
    float mesh_min_x = 0.0F;
    float mesh_min_y = 0.0F;
    float mesh_max_x = 0.0F;
    float mesh_max_y = 0.0F;
  };

  Impl(std::shared_ptr<D3D11Context> context,
       const swim::core::RuntimeAsset& asset,
       const swim::core::AppConfig& config,
       swim::core::RuntimeCounters* metrics,
       D3D11CompletedOutputSink completed_output_sink)
      : context_(std::move(context)),
        logical_width_(asset.logical_width),
        logical_height_(asset.logical_height),
        encoded_width_(asset.encoded_width),
        encoded_height_(asset.encoded_height),
        output_pool_(context_, config.output_pool, encoded_width_,
                     encoded_height_),
        publication_(metrics),
        completed_output_sink_(std::move(completed_output_sink)) {
    if (context_ == nullptr || context_->device == nullptr ||
        context_->immediate_context == nullptr) {
      throw std::invalid_argument("D3D11 renderer requires a valid context");
    }
    if (logical_width_ == 0 || logical_height_ == 0 ||
        encoded_width_ < logical_width_ || encoded_height_ < logical_height_) {
      throw std::invalid_argument("D3D11 renderer dimensions are invalid");
    }
    if (asset.cameras.size() != cameras_.size()) {
      throw std::invalid_argument("D3D11 renderer requires six cameras");
    }
    for (std::size_t index = 0; index < cameras_.size(); ++index) {
      if (asset.cameras[index].camera_id != kCameraOrder[index]) {
        throw std::invalid_argument(
            "D3D11 renderer camera order must be cam3,cam2,cam1,cam4,cam5,cam6");
      }
    }
    publication_.publish([this, output_capacity = config.output_pool]
                         (auto& m) noexcept {
      m.render_inflight_capacity.store(1, std::memory_order_relaxed);
      m.render_output_capacity.store(output_capacity,
                                     std::memory_order_relaxed);
    });

    build_pipelines();
    build_samplers_and_blend();
    upload_camera_resources(asset);
    upload_fullscreen_triangle();
    allocate_accumulation_texture();
    create_completion_query();
  }

  ~Impl() { drain_noexcept(); }

  bool submit(const swim::core::RenderSnapshot& snapshot) noexcept {
    if (fatal_error_.load(std::memory_order_acquire)) {
      return false;
    }
    std::array<D3D11FrameView, 6> frames;
    for (std::size_t index = 0; index < frames.size(); ++index) {
      const auto& lease = snapshot.frames[index];
      if (!lease || lease.metadata().camera_index != index) {
        return false;
      }
      switch (lease.backend_tag()) {
        case kD3D11FrameBackendTag: {
          auto* view =
              static_cast<D3D11FrameView*>(lease.native(kD3D11FrameBackendTag));
          if (view == nullptr) {
            return false;
          }
          frames[index] = *view;
          break;
        }
        case kD3D11DecodedSurfaceTag: {
          auto* view = static_cast<D3D11FrameView*>(
              lease.native(kD3D11DecodedSurfaceTag));
          if (view == nullptr) {
            return false;
          }
          frames[index] = *view;
          break;
        }
        default:
          return false;
      }
      frames[index].metadata = lease.metadata();
    }

    auto output = output_pool_.try_acquire();
    if (!output) {
      record_pool_miss();
      return false;
    }
    publication_.publish([this](auto& m) noexcept {
      m.render_output_high_water.store(output_pool_.high_water(),
                                       std::memory_order_relaxed);
      m.render_output_in_use.store(output_pool_.in_use(),
                                   std::memory_order_relaxed);
    });

    try {
      // The immediate context is not thread-safe; serialize against decode
      // lanes and the preview which share the same device context.
      std::lock_guard lock(context_->context_mutex);
      record_first_submit();
      encode_stitch(frames, output->rtv());
      // Event query + flush gives us a real GPU-completion fence so the output
      // is safe to hand downstream and the input leases can be released.
      auto* ctx = context_->immediate_context.Get();
      ctx->End(completion_query_.Get());
      ctx->Flush();
      BOOL done = FALSE;
      while (ctx->GetData(completion_query_.Get(), &done, sizeof(done), 0) !=
                 S_OK ||
             done != TRUE) {
        std::this_thread::yield();
      }
      record_completion();
      maybe_dump_center_pixel(output->texture());
      maybe_dump_output(output->texture());
      output->anchor_lifetime(shared_from_this());
      if (completed_output_sink_) {
        completed_output_sink_(std::move(*output));
      }
      return true;
    } catch (const std::exception& error) {
      record_fatal(error.what());
      return false;
    } catch (...) {
      record_fatal("unknown D3D11 submission failure");
      return false;
    }
  }

  void drain() {
    flush_completion_metrics();
    if (has_fatal_error()) {
      throw std::runtime_error(fatal_error_message());
    }
  }

  bool has_fatal_error() const noexcept {
    return fatal_error_.load(std::memory_order_acquire);
  }

  std::string fatal_error_message() const {
    std::lock_guard lock(fatal_error_mutex_);
    return fatal_error_message_;
  }

 private:
  void drain_noexcept() noexcept {
    try {
      flush_completion_metrics();
    } catch (...) {
    }
  }

  void record_fatal(std::string message) noexcept {
    try {
      std::lock_guard lock(fatal_error_mutex_);
      if (fatal_error_message_.empty()) {
        fatal_error_message_ = std::move(message);
      }
    } catch (...) {
    }
    fatal_error_.store(true, std::memory_order_release);
  }

  void record_pool_miss() noexcept {
    publication_.publish([](auto& m) noexcept {
      m.pool_exhaustion.fetch_add(1, std::memory_order_relaxed);
      m.render_output_pool_misses.fetch_add(1, std::memory_order_relaxed);
    });
  }

  void record_first_submit() noexcept {
    auto expected = std::uint64_t{0};
    const auto submitted_at = steady_nanoseconds();
    if (first_submit_ns_.compare_exchange_strong(expected, submitted_at,
                                                 std::memory_order_relaxed,
                                                 std::memory_order_relaxed)) {
      publication_.publish([submitted_at](auto& m) noexcept {
        auto zero = std::uint64_t{0};
        m.render_first_submit_ns.compare_exchange_strong(
            zero, submitted_at, std::memory_order_relaxed,
            std::memory_order_relaxed);
      });
    }
  }

  void record_completion() noexcept {
    completed_count_.fetch_add(1, std::memory_order_relaxed);
    const auto completed_at = steady_nanoseconds();
    last_completion_ns_.store(completed_at, std::memory_order_relaxed);
    publication_.publish([completed_at](auto& m) noexcept {
      m.render_completions.fetch_add(1, std::memory_order_relaxed);
      swim::core::record_atomic_max(m.render_last_completion_ns, completed_at);
    });
  }

  void flush_completion_metrics() noexcept {
    publication_.finalize([this](auto& m) noexcept {
      auto expected = std::uint64_t{0};
      m.render_first_submit_ns.compare_exchange_strong(
          expected, first_submit_ns_.load(std::memory_order_relaxed),
          std::memory_order_relaxed, std::memory_order_relaxed);
      swim::core::record_atomic_max(
          m.render_last_completion_ns,
          last_completion_ns_.load(std::memory_order_relaxed));
      m.render_output_in_use.store(output_pool_.in_use(),
                                   std::memory_order_relaxed);
      m.render_output_high_water.store(output_pool_.high_water(),
                                       std::memory_order_relaxed);
    });
  }

  ComPtr<ID3D11Buffer> make_constant_buffer(std::size_t bytes) {
    D3D11_BUFFER_DESC desc{};
    desc.ByteWidth = static_cast<UINT>((bytes + 15U) & ~std::size_t{15});
    desc.Usage = D3D11_USAGE_DYNAMIC;
    desc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    ComPtr<ID3D11Buffer> buffer;
    if (FAILED(context_->device->CreateBuffer(&desc, nullptr,
                                              buffer.GetAddressOf()))) {
      throw std::runtime_error("cannot create D3D11 constant buffer");
    }
    return buffer;
  }

  template <typename T>
  void update_constant_buffer(ID3D11Buffer* buffer, const T& value) {
    D3D11_MAPPED_SUBRESOURCE mapped{};
    if (FAILED(context_->immediate_context->Map(
            buffer, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
      throw std::runtime_error("cannot map D3D11 constant buffer");
    }
    std::memcpy(mapped.pData, &value, sizeof(T));
    context_->immediate_context->Unmap(buffer, 0);
  }

  void build_pipelines() {
    const auto source = read_shader_source();
    auto vs = compile_entry(source, "stitch_vertex", "vs_5_0");
    auto rgba_ps = compile_entry(source, "stitch_rgba", "ps_5_0");
    auto nv12_ps = compile_entry(source, "stitch_nv12", "ps_5_0");
    auto resolve_ps = compile_entry(source, "resolve_accumulation", "ps_5_0");

    auto* device = context_->device.Get();
    if (FAILED(device->CreateVertexShader(vs->GetBufferPointer(),
                                          vs->GetBufferSize(), nullptr,
                                          vertex_shader_.GetAddressOf())) ||
        FAILED(device->CreatePixelShader(rgba_ps->GetBufferPointer(),
                                         rgba_ps->GetBufferSize(), nullptr,
                                         rgba_shader_.GetAddressOf())) ||
        FAILED(device->CreatePixelShader(nv12_ps->GetBufferPointer(),
                                         nv12_ps->GetBufferSize(), nullptr,
                                         nv12_shader_.GetAddressOf())) ||
        FAILED(device->CreatePixelShader(resolve_ps->GetBufferPointer(),
                                         resolve_ps->GetBufferSize(), nullptr,
                                         resolve_shader_.GetAddressOf()))) {
      throw std::runtime_error("cannot create D3D11 shaders");
    }

    const std::array<D3D11_INPUT_ELEMENT_DESC, 2> layout{{
        {"POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0,
         D3D11_INPUT_PER_VERTEX_DATA, 0},
        {"TEXCOORD", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 8,
         D3D11_INPUT_PER_VERTEX_DATA, 0},
    }};
    if (FAILED(device->CreateInputLayout(layout.data(),
                                         static_cast<UINT>(layout.size()),
                                         vs->GetBufferPointer(),
                                         vs->GetBufferSize(),
                                         input_layout_.GetAddressOf()))) {
      throw std::runtime_error("cannot create D3D11 input layout");
    }

    vertex_uniforms_ = make_constant_buffer(sizeof(VertexUniforms));
    fragment_uniforms_ = make_constant_buffer(sizeof(FragmentUniforms));
    resolve_dimensions_ = make_constant_buffer(sizeof(std::array<UINT, 4>));
  }

  void build_samplers_and_blend() {
    auto* device = context_->device.Get();
    D3D11_SAMPLER_DESC clamp_desc{};
    clamp_desc.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    clamp_desc.AddressU = D3D11_TEXTURE_ADDRESS_CLAMP;
    clamp_desc.AddressV = D3D11_TEXTURE_ADDRESS_CLAMP;
    clamp_desc.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    clamp_desc.MaxLOD = D3D11_FLOAT32_MAX;
    if (FAILED(device->CreateSamplerState(&clamp_desc,
                                          sampler_clamp_.GetAddressOf()))) {
      throw std::runtime_error("cannot create D3D11 clamp sampler");
    }
    D3D11_SAMPLER_DESC mirror_desc = clamp_desc;
    mirror_desc.AddressU = D3D11_TEXTURE_ADDRESS_MIRROR;
    mirror_desc.AddressV = D3D11_TEXTURE_ADDRESS_MIRROR;
    mirror_desc.AddressW = D3D11_TEXTURE_ADDRESS_MIRROR;
    if (FAILED(device->CreateSamplerState(&mirror_desc,
                                          sampler_mirror_.GetAddressOf()))) {
      throw std::runtime_error("cannot create D3D11 mirror sampler");
    }

    // Additive blend for the accumulation pass: src*1 + dst*1 on both color and
    // alpha, matching the Metal MTLBlendOperationAdd/One configuration.
    D3D11_BLEND_DESC blend_desc{};
    auto& target = blend_desc.RenderTarget[0];
    target.BlendEnable = TRUE;
    target.SrcBlend = D3D11_BLEND_ONE;
    target.DestBlend = D3D11_BLEND_ONE;
    target.BlendOp = D3D11_BLEND_OP_ADD;
    target.SrcBlendAlpha = D3D11_BLEND_ONE;
    target.DestBlendAlpha = D3D11_BLEND_ONE;
    target.BlendOpAlpha = D3D11_BLEND_OP_ADD;
    target.RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    if (FAILED(device->CreateBlendState(&blend_desc,
                                        additive_blend_.GetAddressOf()))) {
      throw std::runtime_error("cannot create D3D11 additive blend state");
    }
    D3D11_BLEND_DESC opaque_desc{};
    opaque_desc.RenderTarget[0].RenderTargetWriteMask =
        D3D11_COLOR_WRITE_ENABLE_ALL;
    if (FAILED(device->CreateBlendState(&opaque_desc,
                                        opaque_blend_.GetAddressOf()))) {
      throw std::runtime_error("cannot create D3D11 opaque blend state");
    }

    // Metal's default pipeline does not cull; the FBX-derived mesh triangles
    // have no guaranteed winding, so cull nothing here or half the geometry
    // (and the fullscreen resolve triangle) would be dropped, yielding black.
    D3D11_RASTERIZER_DESC raster_desc{};
    raster_desc.FillMode = D3D11_FILL_SOLID;
    raster_desc.CullMode = D3D11_CULL_NONE;
    raster_desc.DepthClipEnable = TRUE;
    if (FAILED(device->CreateRasterizerState(&raster_desc,
                                             rasterizer_.GetAddressOf()))) {
      throw std::runtime_error("cannot create D3D11 rasterizer state");
    }
  }

  void upload_camera_resources(const swim::core::RuntimeAsset& asset) {
    auto* device = context_->device.Get();
    for (std::size_t index = 0; index < cameras_.size(); ++index) {
      const auto& source = asset.cameras[index];
      auto& camera = cameras_[index];
      if (source.vertices.empty() || source.indices.empty() ||
          source.weight_width == 0 || source.weight_height == 0 ||
          source.weights.size() !=
              static_cast<std::size_t>(source.weight_width) *
                  source.weight_height) {
        throw std::invalid_argument("D3D11 camera asset is empty or malformed");
      }
      float mesh_min_x = source.vertices.front().output_x;
      float mesh_max_x = mesh_min_x;
      float mesh_min_y = source.vertices.front().output_y;
      float mesh_max_y = mesh_min_y;
      for (const auto& vertex : source.vertices) {
        mesh_min_x = std::min(mesh_min_x, vertex.output_x);
        mesh_max_x = std::max(mesh_max_x, vertex.output_x);
        mesh_min_y = std::min(mesh_min_y, vertex.output_y);
        mesh_max_y = std::max(mesh_max_y, vertex.output_y);
      }

      D3D11_BUFFER_DESC vertex_desc{};
      vertex_desc.ByteWidth = static_cast<UINT>(
          source.vertices.size() * sizeof(source.vertices[0]));
      vertex_desc.Usage = D3D11_USAGE_IMMUTABLE;
      vertex_desc.BindFlags = D3D11_BIND_VERTEX_BUFFER;
      D3D11_SUBRESOURCE_DATA vertex_data{};
      vertex_data.pSysMem = source.vertices.data();
      if (FAILED(device->CreateBuffer(&vertex_desc, &vertex_data,
                                      camera.vertices.GetAddressOf()))) {
        throw std::runtime_error("cannot upload D3D11 camera vertices");
      }

      D3D11_BUFFER_DESC index_desc{};
      index_desc.ByteWidth = static_cast<UINT>(source.indices.size() *
                                               sizeof(source.indices[0]));
      index_desc.Usage = D3D11_USAGE_IMMUTABLE;
      index_desc.BindFlags = D3D11_BIND_INDEX_BUFFER;
      D3D11_SUBRESOURCE_DATA index_data{};
      index_data.pSysMem = source.indices.data();
      if (FAILED(device->CreateBuffer(&index_desc, &index_data,
                                      camera.indices.GetAddressOf()))) {
        throw std::runtime_error("cannot upload D3D11 camera indices");
      }

      D3D11_TEXTURE2D_DESC weight_desc{};
      weight_desc.Width = source.weight_width;
      weight_desc.Height = source.weight_height;
      weight_desc.MipLevels = 1;
      weight_desc.ArraySize = 1;
      weight_desc.Format = DXGI_FORMAT_R16_UNORM;
      weight_desc.SampleDesc.Count = 1;
      weight_desc.Usage = D3D11_USAGE_IMMUTABLE;
      weight_desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
      D3D11_SUBRESOURCE_DATA weight_data{};
      weight_data.pSysMem = source.weights.data();
      weight_data.SysMemPitch =
          static_cast<UINT>(source.weight_width) * sizeof(source.weights[0]);
      if (FAILED(device->CreateTexture2D(&weight_desc, &weight_data,
                                         camera.weight_texture.GetAddressOf()))) {
        throw std::runtime_error("cannot upload D3D11 feather weights");
      }
      if (FAILED(device->CreateShaderResourceView(
              camera.weight_texture.Get(), nullptr,
              camera.weight_srv.GetAddressOf()))) {
        throw std::runtime_error("cannot create D3D11 feather weight view");
      }

      camera.index_count = static_cast<UINT>(source.indices.size());
      camera.vertex_bytes = source.vertices.size() * sizeof(source.vertices[0]);
      camera.weight_x = static_cast<float>(source.weight_x);
      camera.weight_y = static_cast<float>(source.weight_y);
      camera.weight_width = static_cast<float>(source.weight_width);
      camera.weight_height = static_cast<float>(source.weight_height);
      camera.mesh_min_x = mesh_min_x;
      camera.mesh_min_y = mesh_min_y;
      camera.mesh_max_x = mesh_max_x;
      camera.mesh_max_y = mesh_max_y;
    }
  }

  void upload_fullscreen_triangle() {
    // Oversized triangle covering the whole target. Only output_x/output_y are
    // consumed by the resolve vertex path (offset 0.0, output_size passed in),
    // matching the Metal fullscreen triangle.
    const std::array<swim::core::disk::VertexV1, 3> vertices{{
        {0.0F, 0.0F, 0.0F, 0.0F},
        {2.0F * static_cast<float>(encoded_width_), 0.0F, 0.0F, 0.0F},
        {0.0F, 2.0F * static_cast<float>(encoded_height_), 0.0F, 0.0F},
    }};
    D3D11_BUFFER_DESC desc{};
    desc.ByteWidth = static_cast<UINT>(sizeof(vertices));
    desc.Usage = D3D11_USAGE_IMMUTABLE;
    desc.BindFlags = D3D11_BIND_VERTEX_BUFFER;
    D3D11_SUBRESOURCE_DATA data{};
    data.pSysMem = vertices.data();
    if (FAILED(context_->device->CreateBuffer(
            &desc, &data, fullscreen_vertices_.GetAddressOf()))) {
      throw std::runtime_error("cannot upload D3D11 fullscreen triangle");
    }
  }

  void allocate_accumulation_texture() {
    auto* device = context_->device.Get();
    D3D11_TEXTURE2D_DESC desc{};
    desc.Width = encoded_width_;
    desc.Height = encoded_height_;
    desc.MipLevels = 1;
    desc.ArraySize = 1;
    desc.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
    desc.SampleDesc.Count = 1;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(device->CreateTexture2D(&desc, nullptr,
                                       accumulation_.GetAddressOf())) ||
        FAILED(device->CreateRenderTargetView(
            accumulation_.Get(), nullptr, accumulation_rtv_.GetAddressOf())) ||
        FAILED(device->CreateShaderResourceView(
            accumulation_.Get(), nullptr, accumulation_srv_.GetAddressOf()))) {
      throw std::runtime_error("cannot allocate D3D11 accumulation texture");
    }
  }

  void create_completion_query() {
    D3D11_QUERY_DESC desc{};
    desc.Query = D3D11_QUERY_EVENT;
    if (FAILED(context_->device->CreateQuery(
            &desc, completion_query_.GetAddressOf()))) {
      throw std::runtime_error("cannot create D3D11 completion query");
    }
  }

  // Diagnostic only (SWIM_D3D11_DUMP_PIXEL=1): copy the composite's center
  // pixel to a staging texture and print it, to tell "black output" (renderer
  // bug) apart from "black window" (preview bug). Not on the hot path.
  void maybe_dump_center_pixel(ID3D11Texture2D* output_texture) noexcept {
    static const bool enabled = [] {
      const char* value = std::getenv("SWIM_D3D11_DUMP_PIXEL");
      return value != nullptr && value[0] == '1';
    }();
    if (!enabled || output_texture == nullptr) {
      return;
    }
    static std::atomic_int dumped{0};
    if (dumped.fetch_add(1, std::memory_order_relaxed) >= 3) {
      return;
    }
    try {
      if (staging_pixel_ == nullptr) {
        D3D11_TEXTURE2D_DESC desc{};
        desc.Width = 1;
        desc.Height = 1;
        desc.MipLevels = 1;
        desc.ArraySize = 1;
        desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        desc.SampleDesc.Count = 1;
        desc.Usage = D3D11_USAGE_STAGING;
        desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        context_->device->CreateTexture2D(&desc, nullptr,
                                          staging_pixel_.GetAddressOf());
      }
      if (staging_pixel_ == nullptr) {
        return;
      }
      D3D11_BOX box{};
      box.left = encoded_width_ / 2;
      box.top = encoded_height_ / 2;
      box.front = 0;
      box.right = box.left + 1;
      box.bottom = box.top + 1;
      box.back = 1;
      auto* ctx = context_->immediate_context.Get();
      ctx->CopySubresourceRegion(staging_pixel_.Get(), 0, 0, 0, 0,
                                 output_texture, 0, &box);
      D3D11_MAPPED_SUBRESOURCE mapped{};
      if (SUCCEEDED(ctx->Map(staging_pixel_.Get(), 0, D3D11_MAP_READ, 0,
                             &mapped))) {
        const auto* p = static_cast<const std::uint8_t*>(mapped.pData);
        std::fprintf(stderr, "[d3d11] center BGRA=%u,%u,%u,%u\n", p[0], p[1],
                     p[2], p[3]);
        std::fflush(stderr);
        ctx->Unmap(staging_pixel_.Get(), 0);
      }
    } catch (...) {
    }
  }

  // Diagnostic only (SWIM_DUMP_RAW=<path>): read the full encoded BGRA output
  // back to the CPU once and dump it as raw bytes for the golden comparison.
  void maybe_dump_output(ID3D11Texture2D* output_texture) noexcept {
    static const char* path = std::getenv("SWIM_DUMP_RAW");
    if (path == nullptr || output_texture == nullptr) {
      return;
    }
    static std::atomic_bool done{false};
    bool expected = false;
    if (!done.compare_exchange_strong(expected, true)) {
      return;
    }
    try {
      D3D11_TEXTURE2D_DESC desc{};
      desc.Width = encoded_width_;
      desc.Height = encoded_height_;
      desc.MipLevels = 1;
      desc.ArraySize = 1;
      desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
      desc.SampleDesc.Count = 1;
      desc.Usage = D3D11_USAGE_STAGING;
      desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
      ComPtr<ID3D11Texture2D> staging;
      if (FAILED(context_->device->CreateTexture2D(&desc, nullptr,
                                                   staging.GetAddressOf()))) {
        return;
      }
      auto* ctx = context_->immediate_context.Get();
      ctx->CopyResource(staging.Get(), output_texture);
      D3D11_MAPPED_SUBRESOURCE mapped{};
      if (FAILED(ctx->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped))) {
        return;
      }
      std::ofstream out(path, std::ios::binary);
      const auto* base = static_cast<const std::uint8_t*>(mapped.pData);
      for (std::uint32_t row = 0; row < encoded_height_; ++row) {
        out.write(reinterpret_cast<const char*>(base + row * mapped.RowPitch),
                  static_cast<std::streamsize>(encoded_width_) * 4);
      }
      ctx->Unmap(staging.Get(), 0);
      std::fprintf(stderr, "[d3d11] dumped raw BGRA output to %s\n", path);
      std::fflush(stderr);
    } catch (...) {
    }
  }

  void set_viewport(UINT width, UINT height) {
    D3D11_VIEWPORT viewport{};
    viewport.Width = static_cast<float>(width);
    viewport.Height = static_cast<float>(height);
    viewport.MaxDepth = 1.0F;
    context_->immediate_context->RSSetViewports(1, &viewport);
  }

  void encode_stitch(const std::array<D3D11FrameView, 6>& frames,
                     ID3D11RenderTargetView* output_rtv) {
    auto* ctx = context_->immediate_context.Get();
    const float clear[4] = {0.0F, 0.0F, 0.0F, 0.0F};
    ctx->ClearRenderTargetView(accumulation_rtv_.Get(), clear);

    ID3D11RenderTargetView* accum_rtv = accumulation_rtv_.Get();
    ctx->OMSetRenderTargets(1, &accum_rtv, nullptr);
    const float blend_factor[4] = {0.0F, 0.0F, 0.0F, 0.0F};
    ctx->OMSetBlendState(additive_blend_.Get(), blend_factor, 0xffffffffU);
    ctx->RSSetState(rasterizer_.Get());
    ctx->IASetInputLayout(input_layout_.Get());
    ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(vertex_shader_.Get(), nullptr, 0);
    set_viewport(encoded_width_, encoded_height_);

    ID3D11SamplerState* samplers[2] = {sampler_clamp_.Get(),
                                       sampler_mirror_.Get()};
    ctx->PSSetSamplers(0, 2, samplers);

    for (std::size_t index = 0; index < cameras_.size(); ++index) {
      const auto& camera = cameras_[index];
      const auto& frame = frames[index];
      float texel_x = 0.0F;
      float texel_y = 0.0F;
      const bool is_rgba = frame.rgba != nullptr;
      D3D11_TEXTURE2D_DESC tex_desc{};
      {
        ComPtr<ID3D11Resource> resource;
        if (is_rgba) {
          frame.rgba->GetResource(resource.GetAddressOf());
        } else {
          if (frame.luma == nullptr || frame.chroma == nullptr) {
            throw std::invalid_argument("D3D11 NV12 planes are unavailable");
          }
          frame.luma->GetResource(resource.GetAddressOf());
        }
        ComPtr<ID3D11Texture2D> tex;
        if (resource == nullptr || FAILED(resource.As(&tex)) || tex == nullptr) {
          throw std::invalid_argument("D3D11 input texture is unavailable");
        }
        tex->GetDesc(&tex_desc);
      }
      if (tex_desc.Width == 0 || tex_desc.Height == 0) {
        throw std::invalid_argument("D3D11 input texture is unavailable");
      }
      texel_x = 1.0F / static_cast<float>(tex_desc.Width);
      texel_y = 1.0F / static_cast<float>(tex_desc.Height);

      const FragmentUniforms fragment_uniforms{
          texel_x, texel_y, camera.weight_x, camera.weight_y,
          camera.weight_width, camera.weight_height,
          static_cast<std::uint32_t>(frame.metadata.color_matrix),
          frame.metadata.pixel_format ==
                  swim::core::PixelFormat::nv12_full_range
              ? 1U
              : 0U};
      const VertexUniforms vertex_uniforms{
          static_cast<float>(encoded_width_),
          static_cast<float>(encoded_height_),
          0.5F,
          0.5F,
          camera.mesh_min_x,
          camera.mesh_min_y,
          camera.mesh_max_x,
          camera.mesh_max_y,
          kPerimeterTolerance,
          kInclusiveExpansion,
          1U,
          0U};
      update_constant_buffer(vertex_uniforms_.Get(), vertex_uniforms);
      update_constant_buffer(fragment_uniforms_.Get(), fragment_uniforms);

      const UINT stride = sizeof(swim::core::disk::VertexV1);
      const UINT offset = 0;
      ID3D11Buffer* vertex_buffer = camera.vertices.Get();
      ctx->IASetVertexBuffers(0, 1, &vertex_buffer, &stride, &offset);
      ctx->IASetIndexBuffer(camera.indices.Get(), DXGI_FORMAT_R32_UINT, 0);
      ID3D11Buffer* vs_cb = vertex_uniforms_.Get();
      ctx->VSSetConstantBuffers(0, 1, &vs_cb);
      ID3D11Buffer* ps_cb = fragment_uniforms_.Get();
      ctx->PSSetConstantBuffers(0, 1, &ps_cb);

      if (is_rgba) {
        ctx->PSSetShader(rgba_shader_.Get(), nullptr, 0);
        ID3D11ShaderResourceView* views[2] = {frame.rgba, camera.weight_srv.Get()};
        ctx->PSSetShaderResources(0, 2, views);
      } else {
        ctx->PSSetShader(nv12_shader_.Get(), nullptr, 0);
        ID3D11ShaderResourceView* views[3] = {frame.luma, frame.chroma,
                                              camera.weight_srv.Get()};
        ctx->PSSetShaderResources(0, 3, views);
      }
      ctx->DrawIndexed(camera.index_count, 0, 0);

      // Unbind SRVs so the next pass can bind the accumulation texture as input.
      ID3D11ShaderResourceView* null_views[3] = {nullptr, nullptr, nullptr};
      ctx->PSSetShaderResources(0, 3, null_views);
    }

    // Resolve pass: normalize rgb/alpha into the BGRA output. Full-screen
    // triangle via SV_VertexID would need a separate VS; instead reuse the
    // stitch VS with a fullscreen quad expressed through the resolve dimensions
    // in the pixel shader, driven by a 3-vertex draw with no vertex buffer.
    ID3D11RenderTargetView* rtvs[1] = {output_rtv};
    ctx->OMSetRenderTargets(1, rtvs, nullptr);
    ctx->OMSetBlendState(opaque_blend_.Get(), blend_factor, 0xffffffffU);
    set_viewport(encoded_width_, encoded_height_);

    const std::array<UINT, 4> dimensions{logical_width_, logical_height_,
                                          encoded_width_, encoded_height_};
    update_constant_buffer(resolve_dimensions_.Get(), dimensions);

    // Fullscreen triangle emitted from the resolve vertex path. We provide a
    // 3-vertex NDC triangle through a tiny immutable vertex buffer.
    const UINT stride = sizeof(swim::core::disk::VertexV1);
    const UINT offset = 0;
    ID3D11Buffer* fullscreen = fullscreen_vertices_.Get();
    ctx->IASetVertexBuffers(0, 1, &fullscreen, &stride, &offset);
    VertexUniforms resolve_vertex{static_cast<float>(encoded_width_),
                                  static_cast<float>(encoded_height_),
                                  0.0F, 0.0F, 0.0F, 0.0F, 0.0F, 0.0F,
                                  0.0F, 0.0F, 0U, 0U};
    update_constant_buffer(vertex_uniforms_.Get(), resolve_vertex);
    ID3D11Buffer* vs_cb = vertex_uniforms_.Get();
    ctx->VSSetConstantBuffers(0, 1, &vs_cb);
    ID3D11Buffer* resolve_cb = resolve_dimensions_.Get();
    ctx->PSSetConstantBuffers(0, 1, &resolve_cb);
    ctx->PSSetShader(resolve_shader_.Get(), nullptr, 0);
    ID3D11ShaderResourceView* accum_srv = accumulation_srv_.Get();
    ctx->PSSetShaderResources(0, 1, &accum_srv);
    ctx->Draw(3, 0);
    ID3D11ShaderResourceView* null_srv[1] = {nullptr};
    ctx->PSSetShaderResources(0, 1, null_srv);
    ID3D11RenderTargetView* null_rtv[1] = {nullptr};
    ctx->OMSetRenderTargets(1, null_rtv, nullptr);
  }

  std::shared_ptr<D3D11Context> context_;
  std::uint32_t logical_width_;
  std::uint32_t logical_height_;
  std::uint32_t encoded_width_;
  std::uint32_t encoded_height_;
  std::array<CameraResources, 6> cameras_;

  ComPtr<ID3D11VertexShader> vertex_shader_;
  ComPtr<ID3D11PixelShader> rgba_shader_;
  ComPtr<ID3D11PixelShader> nv12_shader_;
  ComPtr<ID3D11PixelShader> resolve_shader_;
  ComPtr<ID3D11InputLayout> input_layout_;
  ComPtr<ID3D11Buffer> vertex_uniforms_;
  ComPtr<ID3D11Buffer> fragment_uniforms_;
  ComPtr<ID3D11Buffer> resolve_dimensions_;
  ComPtr<ID3D11Buffer> fullscreen_vertices_;
  ComPtr<ID3D11SamplerState> sampler_clamp_;
  ComPtr<ID3D11SamplerState> sampler_mirror_;
  ComPtr<ID3D11BlendState> additive_blend_;
  ComPtr<ID3D11BlendState> opaque_blend_;
  ComPtr<ID3D11RasterizerState> rasterizer_;
  ComPtr<ID3D11Texture2D> accumulation_;
  ComPtr<ID3D11RenderTargetView> accumulation_rtv_;
  ComPtr<ID3D11ShaderResourceView> accumulation_srv_;
  ComPtr<ID3D11Query> completion_query_;
  ComPtr<ID3D11Texture2D> staging_pixel_;

  D3D11OutputPool output_pool_;
  swim::core::RuntimeCounterPublication publication_;
  D3D11CompletedOutputSink completed_output_sink_;
  std::atomic_uint64_t completed_count_{0};
  std::atomic_uint64_t first_submit_ns_{0};
  std::atomic_uint64_t last_completion_ns_{0};
  std::atomic_bool fatal_error_{false};
  mutable std::mutex fatal_error_mutex_;
  std::string fatal_error_message_;
};

D3D11StitchRenderer::D3D11StitchRenderer(
    std::shared_ptr<D3D11Context> context,
    const swim::core::RuntimeAsset& asset,
    const swim::core::AppConfig& config,
    swim::core::RuntimeCounters* metrics,
    D3D11CompletedOutputSink completed_output_sink)
    : impl_(std::make_shared<Impl>(std::move(context), asset, config, metrics,
                                   std::move(completed_output_sink))) {}

D3D11StitchRenderer::~D3D11StitchRenderer() {
  if (impl_ != nullptr) {
    try {
      impl_->drain();
    } catch (...) {
    }
  }
}

bool D3D11StitchRenderer::submit(
    const swim::core::RenderSnapshot& snapshot) noexcept {
  return impl_->submit(snapshot);
}

void D3D11StitchRenderer::drain() { impl_->drain(); }

bool D3D11StitchRenderer::has_fatal_error() const noexcept {
  return impl_->has_fatal_error();
}

std::string D3D11StitchRenderer::fatal_error_message() const {
  return impl_->fatal_error_message();
}

}  // namespace swim::d3d11



