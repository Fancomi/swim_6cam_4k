#include <swim/d3d11/d3d11_backend.hpp>

#include <swim/core/camera_capacity.hpp>
#include <swim/core/backend.hpp>
#include <swim/core/benchmark_stage.hpp>
#include <swim/d3d11/d3d11_frame.hpp>
#include <swim/d3d11/d3d11_preview.hpp>
#include <swim/d3d11/d3d11_renderer.hpp>
#include <swim/d3d11/mf_source.hpp>

#include <array>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace swim::d3d11 {
namespace {

std::shared_ptr<D3D11Context> make_context() {
  auto context = std::make_shared<D3D11Context>();
  UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
  // Media Foundation hardware decode requires the device to support
  // multithreaded protection since decoder callbacks touch it off-thread.
  const std::array<D3D_FEATURE_LEVEL, 2> levels{D3D_FEATURE_LEVEL_11_1,
                                                D3D_FEATURE_LEVEL_11_0};
  D3D_FEATURE_LEVEL obtained{};
  auto hr = D3D11CreateDevice(
      nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, flags, levels.data(),
      static_cast<UINT>(levels.size()), D3D11_SDK_VERSION,
      context->device.GetAddressOf(), &obtained,
      context->immediate_context.GetAddressOf());
  if (FAILED(hr)) {
    throw std::runtime_error("cannot create D3D11 device");
  }

  // Enable multithread protection on the immediate context so it is safe to
  // share the device between the render thread and MF decode lanes.
  ComPtr<ID3D10Multithread> multithread;
  if (SUCCEEDED(context->immediate_context.As(&multithread))) {
    multithread->SetMultithreadProtected(TRUE);
  }

  ComPtr<IDXGIDevice> dxgi_device;
  if (FAILED(context->device.As(&dxgi_device))) {
    throw std::runtime_error("cannot obtain DXGI device");
  }
  ComPtr<IDXGIAdapter> adapter;
  if (FAILED(dxgi_device->GetAdapter(adapter.GetAddressOf()))) {
    throw std::runtime_error("cannot obtain DXGI adapter");
  }
  if (FAILED(adapter->GetParent(__uuidof(IDXGIFactory2),
                                reinterpret_cast<void**>(
                                    context->factory.GetAddressOf())))) {
    throw std::runtime_error("cannot obtain DXGI factory");
  }
  return context;
}

void retain_static_frame(void*) noexcept {}
void release_static_frame(void*) noexcept {}

// Placeholder camera surfaces for render-only/benchmark stages and for the
// black replacement of a failed lane. Each is a solid BGRA texture wrapped in a
// D3D11FrameView so the stitch renderer takes the rgba path.
class PlaceholderFrames final {
 public:
  PlaceholderFrames(const std::shared_ptr<D3D11Context>& context,
                    std::uint32_t width, std::uint32_t height,
                    std::uint32_t camera_count)
      : camera_count_(camera_count) {
    if (camera_count_ == 0 || camera_count_ > swim::core::kMaxCameras) {
      throw std::invalid_argument(
          "D3D11 placeholder lane count must be between 1 and kMaxCameras");
    }
    for (std::uint32_t camera = 0; camera < camera_count_; ++camera) {
      // Distinct grey levels per lane so a synthetic render shows separate
      // panels rather than a flat frame.
      const std::uint8_t level = static_cast<std::uint8_t>(
          40 + camera * (200 / camera_count_));
      create_solid(context, width, height, level, camera);
      views_[camera].rgba = srvs_[camera].Get();
      views_[camera].metadata.camera_index = camera;
      views_[camera].metadata.width = width;
      views_[camera].metadata.height = height;
      views_[camera].metadata.pixel_format = swim::core::PixelFormat::bgra8;
    }
  }

  swim::core::FrameLease lease(std::uint32_t camera_index) const {
    if (camera_index >= camera_count_) {
      return {};
    }
    return swim::core::FrameLease{
        const_cast<D3D11FrameView*>(&views_[camera_index]),
        {retain_static_frame, release_static_frame, kD3D11FrameBackendTag},
        views_[camera_index].metadata};
  }

 private:
  void create_solid(const std::shared_ptr<D3D11Context>& context,
                    std::uint32_t width, std::uint32_t height,
                    std::uint8_t level, std::uint32_t camera) {
    std::vector<std::uint32_t> pixels(
        static_cast<std::size_t>(width) * height,
        0xff000000U | (static_cast<std::uint32_t>(level) << 16) |
            (static_cast<std::uint32_t>(level) << 8) | level);
    D3D11_TEXTURE2D_DESC desc{};
    desc.Width = width;
    desc.Height = height;
    desc.MipLevels = 1;
    desc.ArraySize = 1;
    desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.Usage = D3D11_USAGE_IMMUTABLE;
    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    D3D11_SUBRESOURCE_DATA data{};
    data.pSysMem = pixels.data();
    data.SysMemPitch = width * 4U;
    if (FAILED(context->device->CreateTexture2D(
            &desc, &data, textures_[camera].GetAddressOf()))) {
      throw std::runtime_error("cannot create D3D11 placeholder texture");
    }
    if (FAILED(context->device->CreateShaderResourceView(
            textures_[camera].Get(), nullptr, srvs_[camera].GetAddressOf()))) {
      throw std::runtime_error("cannot create D3D11 placeholder view");
    }
  }

  std::uint32_t camera_count_{};
  std::array<ComPtr<ID3D11Texture2D>, swim::core::kMaxCameras> textures_;
  std::array<ComPtr<ID3D11ShaderResourceView>, swim::core::kMaxCameras> srvs_;
  std::array<D3D11FrameView, swim::core::kMaxCameras> views_;
};

class D3D11RendererAdapter final : public swim::core::IRenderer {
 public:
  D3D11RendererAdapter(std::shared_ptr<D3D11Context> context,
                       const swim::core::RuntimeAsset& asset,
                       const swim::core::AppConfig& config,
                       swim::core::RuntimeCounters& metrics,
                       std::shared_ptr<D3D11Preview> preview)
      : preview_(std::move(preview)),
        placeholders_(context, asset.logical_width, asset.logical_height,
                      static_cast<std::uint32_t>(asset.cameras.size())),
        renderer_(context, asset, config, &metrics,
                  make_sink(preview_)) {}

  swim::core::RenderSubmitResult submit(
      const swim::core::RenderSnapshot& snapshot) override {
    for (std::size_t camera = 0; camera < snapshot.camera_count; ++camera) {
      const auto& lease = snapshot.frames[camera];
      if (!lease) {
        return swim::core::RenderSubmitResult::not_ready;
      }
      if (lease.metadata().camera_index != camera) {
        return swim::core::RenderSubmitResult::invalid;
      }
    }
    if (renderer_.submit(snapshot)) {
      return swim::core::RenderSubmitResult::accepted;
    }
    return renderer_.has_fatal_error()
               ? swim::core::RenderSubmitResult::fatal
               : swim::core::RenderSubmitResult::backpressure;
  }

  swim::core::FrameLease replacement_frame(
      std::uint32_t camera_index) const override {
    return placeholders_.lease(camera_index);
  }

  swim::core::FrameLease benchmark_frame(
      std::uint32_t camera_index) const override {
    return placeholders_.lease(camera_index);
  }

  void drain() override { renderer_.drain(); }

  bool has_fatal_error() const noexcept override {
    return renderer_.has_fatal_error();
  }

  std::string last_error() const override {
    return renderer_.fatal_error_message();
  }

 private:
  static D3D11CompletedOutputSink make_sink(
      const std::shared_ptr<D3D11Preview>& preview) {
    if (preview == nullptr) {
      return {};
    }
    std::weak_ptr<D3D11Preview> weak = preview;
    return [weak](D3D11OutputLease output) {
      if (auto locked = weak.lock()) {
        static_cast<void>(locked->offer(std::move(output)));
      }
    };
  }

  std::shared_ptr<D3D11Preview> preview_;
  PlaceholderFrames placeholders_;
  D3D11StitchRenderer renderer_;
};

class D3D11SourceAdapter final : public swim::core::ISource {
 public:
  D3D11SourceAdapter(std::shared_ptr<D3D11Context> context,
                     swim::core::SourceConfig source,
                     std::uint32_t camera_index,
                     const swim::core::AppConfig& config,
                     swim::core::RuntimeCounters& metrics,
                     swim::core::RunLifecycle& lifecycle,
                     swim::core::SharedLaneOrigin& shared_origin)
      : context_(std::move(context)),
        source_(std::move(source)),
        camera_index_(camera_index),
        config_(config),
        metrics_(metrics),
        lifecycle_(lifecycle),
        shared_origin_(shared_origin) {}

  void start(swim::core::LatestFrameMailbox& output) override {
    source_impl_ = std::make_unique<MfSource>(
        context_, source_, camera_index_, output, metrics_, config_.mode,
        config_.decode_ticket_pool, config_.decode_surface_pool, &lifecycle_,
        config_.loop_sources, config_.stop_at_eof, config_.loop_period,
        &shared_origin_);
    source_impl_->start();
  }

  void stop() noexcept override {
    if (source_impl_ == nullptr) {
      return;
    }
    source_impl_->stop();
    try {
      source_impl_->wait();
    } catch (...) {
    }
  }

  bool failed() const noexcept override {
    return source_impl_ != nullptr && source_impl_->failed();
  }

  std::string last_error() const override {
    return source_impl_ == nullptr ? std::string{} : source_impl_->last_error();
  }

 private:
  std::shared_ptr<D3D11Context> context_;
  swim::core::SourceConfig source_;
  std::uint32_t camera_index_;
  swim::core::AppConfig config_;
  swim::core::RuntimeCounters& metrics_;
  swim::core::RunLifecycle& lifecycle_;
  swim::core::SharedLaneOrigin& shared_origin_;
  std::unique_ptr<MfSource> source_impl_;
};

class D3D11Backend final : public swim::core::IBackend {
 public:
  D3D11Backend() : context_(make_context()) {}

  void bind_metrics(swim::core::RuntimeCounters& metrics) noexcept override {
    metrics_ = &metrics;
  }

  void bind_lifecycle(swim::core::RunLifecycle& lifecycle) noexcept override {
    lifecycle_ = &lifecycle;
  }

  std::unique_ptr<swim::core::ISource> make_source(
      const swim::core::SourceConfig& source,
      std::uint32_t camera_index) override {
    if (!config_ready_) {
      throw std::logic_error("D3D11 renderer must be created before sources");
    }
    if (lifecycle_ == nullptr) {
      throw std::logic_error("D3D11 lifecycle must be bound before sources");
    }
    return std::make_unique<D3D11SourceAdapter>(
        context_, source, camera_index, config_, *metrics_, *lifecycle_,
        shared_origin_);
  }

  std::unique_ptr<swim::core::IRenderer> make_renderer(
      const swim::core::RuntimeAsset& asset,
      const swim::core::AppConfig& config,
      const swim::core::BenchmarkGraph& graph) override {
    config_ = config;
    config_ready_ = true;
    if (!graph.create_renderer) {
      preview_.reset();
      return {};
    }
    if (graph.preview) {
      preview_ = std::make_shared<D3D11Preview>(
          context_, asset.encoded_width, asset.encoded_height, *metrics_,
          [this] { stop_main_loop(); }, config.preview_visible);
    } else {
      preview_.reset();
    }
    return std::make_unique<D3D11RendererAdapter>(context_, asset, config,
                                                  *metrics_, preview_);
  }

  void run_main_loop(std::stop_token token) override {
    if (preview_ != nullptr) {
      preview_->run_main_loop(token);
      return;
    }
    std::unique_lock lock(loop_mutex_);
    loop_condition_.wait(lock, token, [this] { return loop_stopped_; });
  }

  void stop_main_loop() noexcept override {
    {
      std::lock_guard lock(loop_mutex_);
      loop_stopped_ = true;
    }
    loop_condition_.notify_all();
    if (preview_ != nullptr) {
      preview_->request_stop();
    }
  }

 private:
  std::shared_ptr<D3D11Context> context_;
  std::shared_ptr<D3D11Preview> preview_;
  swim::core::AppConfig config_;
  swim::core::RuntimeCounters fallback_metrics_;
  swim::core::RuntimeCounters* metrics_{&fallback_metrics_};
  swim::core::RunLifecycle* lifecycle_{};
  // One wall anchor for media t=0 across every lane, so lanes that open their
  // readers at different speeds do not each freeze in their own stagger.
  swim::core::SharedLaneOrigin shared_origin_;
  bool config_ready_{};
  std::mutex loop_mutex_;
  std::condition_variable_any loop_condition_;
  bool loop_stopped_{};
};

std::unique_ptr<swim::core::IBackend> make_d3d11_backend() {
  return std::make_unique<D3D11Backend>();
}

}  // namespace

void register_d3d11_backend() {
  static std::once_flag once;
  std::call_once(once, [] {
    swim::core::BackendRegistry::instance().register_factory(
        "d3d11", &make_d3d11_backend);
  });
}

}  // namespace swim::d3d11

