#include <swim/metal/metal_backend.hpp>

#include <swim/core/backend.hpp>
#include <swim/core/benchmark_stage.hpp>
#include <swim/metal/metal_encoder.hpp>
#include <swim/metal/metal_preview.hpp>
#include <swim/metal/metal_renderer.hpp>
#include <swim/metal/mp4_source.hpp>

#import <CoreVideo/CoreVideo.h>
#import <Metal/Metal.h>

#include <array>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <exception>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <utility>

namespace swim::metal {

bool metal_encoder_admits_render(const MetalEncoder& encoder) noexcept {
  return !encoder.has_fatal_error();
}

namespace {

std::shared_ptr<MetalContext> make_context() {
  auto context = std::make_shared<MetalContext>();
  context->device = MTLCreateSystemDefaultDevice();
  if (context->device == nil) {
    throw std::runtime_error("Metal device is unavailable");
  }
  context->command_queue = [context->device newCommandQueue];
  if (context->command_queue == nil) {
    throw std::runtime_error("cannot create Metal command queue");
  }
  const auto status = CVMetalTextureCacheCreate(
      kCFAllocatorDefault, nullptr, context->device, nullptr,
      &context->texture_cache);
  if (status != kCVReturnSuccess || context->texture_cache == nullptr) {
    throw std::runtime_error("cannot create shared Metal texture cache");
  }
  return context;
}

void retain_static_frame(void*) noexcept {}
void release_static_frame(void*) noexcept {}

MetalCompletedOutputSink router_sink(
    const std::shared_ptr<MetalCompletedOutputRouter>& router) {
  if (router == nullptr) {
    return {};
  }
  const std::weak_ptr<MetalCompletedOutputRouter> weak = router;
  return [weak](MetalOutputLease output) {
    if (auto owner = weak.lock()) {
      static_cast<void>(owner->route(std::move(output)));
    }
  };
}

class MetalRendererAdapter final : public swim::core::IRenderer {
 public:
  MetalRendererAdapter(std::shared_ptr<MetalContext> context,
                       const swim::core::RuntimeAsset& asset,
                       const swim::core::AppConfig& config,
                       swim::core::RuntimeCounters& metrics,
                       std::shared_ptr<MetalCompletedOutputRouter> router,
                       std::shared_ptr<MetalPreview> preview,
                       std::shared_ptr<MetalEncoder> encoder)
      : context_(std::move(context)),
        router_(std::move(router)),
        preview_(std::move(preview)),
        encoder_(std::move(encoder)),
        camera_count_(static_cast<std::uint32_t>(asset.cameras.size())),
        renderer_(context_, asset, config, &metrics, router_sink(router_)) {
    // Synthetic stand-ins for render-only/benchmark stages. They only need to
    // cover the stitch's UV sampling, so the asset's logical size is the natural
    // choice — no dependence on any particular camera resolution.
    auto* descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                    width:asset.logical_width
                                   height:asset.logical_height
                                mipmapped:NO];
    descriptor.storageMode = MTLStorageModePrivate;
    descriptor.usage =
        MTLTextureUsageShaderRead | MTLTextureUsageRenderTarget;
    id<MTLCommandBuffer> command_buffer =
        [context_->command_queue commandBuffer];
    if (command_buffer == nil) {
      throw std::runtime_error(
          "cannot create Metal benchmark-frame command buffer");
    }
    for (std::uint32_t camera = 0; camera < camera_count_; ++camera) {
      benchmark_textures_[camera] =
          [context_->device newTextureWithDescriptor:descriptor];
      if (benchmark_textures_[camera] == nil) {
        throw std::runtime_error("cannot create Metal benchmark texture");
      }
      auto* pass = [MTLRenderPassDescriptor renderPassDescriptor];
      pass.colorAttachments[0].texture = benchmark_textures_[camera];
      pass.colorAttachments[0].loadAction = MTLLoadActionClear;
      pass.colorAttachments[0].storeAction = MTLStoreActionStore;
      pass.colorAttachments[0].clearColor = MTLClearColorMake(0, 0, 0, 1);
      id<MTLRenderCommandEncoder> clear_encoder =
          [command_buffer renderCommandEncoderWithDescriptor:pass];
      if (clear_encoder == nil) {
        throw std::runtime_error(
            "cannot create Metal benchmark-frame clear encoder");
      }
      [clear_encoder endEncoding];

      benchmark_frames_[camera].rgba = benchmark_textures_[camera];
      benchmark_frames_[camera].metadata.camera_index = camera;
      benchmark_frames_[camera].metadata.width = asset.logical_width;
      benchmark_frames_[camera].metadata.height = asset.logical_height;
      benchmark_frames_[camera].metadata.pixel_format =
          swim::core::PixelFormat::bgra8;
    }
    [command_buffer commit];
    [command_buffer waitUntilCompleted];
    if (command_buffer.status != MTLCommandBufferStatusCompleted) {
      throw std::runtime_error("Metal benchmark-frame initialization failed");
    }
  }

  swim::core::RenderSubmitResult submit(
      const swim::core::RenderSnapshot& snapshot) override {
    if (encoder_ != nullptr && !metal_encoder_admits_render(*encoder_)) {
      return swim::core::RenderSubmitResult::fatal;
    }
    for (std::size_t camera = 0; camera < snapshot.camera_count; ++camera) {
      const auto& lease = snapshot.frames[camera];
      if (!lease) {
        return swim::core::RenderSubmitResult::not_ready;
      }
      if (lease.metadata().camera_index != camera) {
        return swim::core::RenderSubmitResult::invalid;
      }
      if (lease.metadata().width == 0 || lease.metadata().height == 0) {
        return swim::core::RenderSubmitResult::invalid;
      }
      if (lease.backend_tag() == kMetalDecodedSurfaceTag) {
        auto* surface = static_cast<MetalDecodedSurface*>(
            lease.native(kMetalDecodedSurfaceTag));
        if (surface == nullptr || surface->camera_index != camera ||
            surface->luma == nil || surface->chroma == nil ||
            lease.metadata().pixel_format == swim::core::PixelFormat::bgra8) {
          return swim::core::RenderSubmitResult::invalid;
        }
      } else if (lease.backend_tag() == kMetalFrameBackendTag) {
        auto* view = static_cast<MetalFrameView*>(
            lease.native(kMetalFrameBackendTag));
        const bool valid_bgra =
            lease.metadata().pixel_format == swim::core::PixelFormat::bgra8 &&
            view != nullptr && view->rgba != nil;
        const bool valid_nv12 =
            lease.metadata().pixel_format != swim::core::PixelFormat::bgra8 &&
            view != nullptr && view->luma != nil && view->chroma != nil;
        if (!valid_bgra && !valid_nv12) {
          return swim::core::RenderSubmitResult::invalid;
        }
      } else {
        return swim::core::RenderSubmitResult::invalid;
      }
    }
    MetalRenderResult result;
    const auto accepted = renderer_.submit(snapshot, result);
    if (accepted) {
      return swim::core::RenderSubmitResult::accepted;
    }
    return renderer_.has_fatal_error()
               ? swim::core::RenderSubmitResult::fatal
               : swim::core::RenderSubmitResult::backpressure;
  }

  swim::core::FrameLease replacement_frame(
      std::uint32_t camera_index) const override {
    return benchmark_frame(camera_index);
  }

  swim::core::FrameLease benchmark_frame(
      std::uint32_t camera_index) const override {
    if (camera_index >= camera_count_) {
      return {};
    }
    return swim::core::FrameLease{
        const_cast<MetalFrameView*>(&benchmark_frames_[camera_index]),
        {retain_static_frame, release_static_frame, kMetalFrameBackendTag},
        benchmark_frames_[camera_index].metadata};
  }

  void drain() override {
    std::exception_ptr error;
    try {
      renderer_.drain();
    } catch (...) {
      error = std::current_exception();
    }
    try {
      if (router_ != nullptr) {
        router_->close_and_flush();
      }
    } catch (...) {
      if (!error) {
        error = std::current_exception();
      }
    }
    try {
      if (preview_ != nullptr) {
        preview_->close_and_drain();
      }
    } catch (...) {
      if (!error) {
        error = std::current_exception();
      }
    }
    try {
      if (encoder_ != nullptr) {
        encoder_->close_and_drain();
      }
    } catch (...) {
      if (!error) {
        error = std::current_exception();
      }
    }
    if (error) {
      std::rethrow_exception(error);
    }
  }
  bool has_fatal_error() const noexcept override {
    return renderer_.has_fatal_error() ||
           (encoder_ != nullptr && encoder_->has_fatal_error());
  }
  std::string last_error() const override {
    if (renderer_.has_fatal_error()) {
      return renderer_.fatal_error_message();
    }
    return encoder_ == nullptr ? std::string{}
                               : encoder_->fatal_error_message();
  }

 private:
  std::shared_ptr<MetalContext> context_;
  std::shared_ptr<MetalCompletedOutputRouter> router_;
  std::shared_ptr<MetalPreview> preview_;
  std::shared_ptr<MetalEncoder> encoder_;
  std::uint32_t camera_count_{};
  MetalStitchRenderer renderer_;
  std::array<id<MTLTexture>, swim::core::kMaxCameras> benchmark_textures_{};
  std::array<MetalFrameView, swim::core::kMaxCameras> benchmark_frames_;
};

class MetalSourceAdapter final : public swim::core::ISource {
 public:
  MetalSourceAdapter(std::shared_ptr<MetalContext> context,
                     swim::core::SourceConfig source,
                     std::uint32_t camera_index,
                     const swim::core::AppConfig& config,
                     swim::core::RuntimeCounters& metrics,
                     swim::core::RunLifecycle& lifecycle)
      : context_(std::move(context)),
        source_(std::move(source)),
        camera_index_(camera_index),
        config_(config),
        metrics_(metrics),
        lifecycle_(lifecycle) {}

  void start(swim::core::LatestFrameMailbox& output) override {
    source_impl_ = std::make_unique<Mp4VideoToolboxSource>(
        context_, source_, camera_index_, output, metrics_, config_.mode,
        std::chrono::milliseconds{0},
        config_.decode_ticket_pool, config_.decode_surface_pool, &lifecycle_,
        config_.loop_sources, config_.loop_period, config_.stop_at_eof);
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
  std::shared_ptr<MetalContext> context_;
  swim::core::SourceConfig source_;
  std::uint32_t camera_index_;
  swim::core::AppConfig config_;
  swim::core::RuntimeCounters& metrics_;
  swim::core::RunLifecycle& lifecycle_;
  std::unique_ptr<Mp4VideoToolboxSource> source_impl_;
};

class MetalBackend final : public swim::core::IBackend {
 public:
  MetalBackend() : context_(make_context()) {}

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
      throw std::logic_error("Metal renderer must be created before sources");
    }
    if (lifecycle_ == nullptr) {
      throw std::logic_error("Metal lifecycle must be bound before sources");
    }
    return std::make_unique<MetalSourceAdapter>(
        context_, source, camera_index, config_, *metrics_, *lifecycle_);
  }

  std::unique_ptr<swim::core::IRenderer> make_renderer(
      const swim::core::RuntimeAsset& asset,
      const swim::core::AppConfig& config,
      const swim::core::BenchmarkGraph& graph) override {
    config_ = config;
    config_ready_ = true;
    if (!graph.create_renderer) {
      router_.reset();
      preview_.reset();
      encoder_.reset();
      return {};
    }
    if (graph.preview || graph.encode) {
      router_ = std::make_shared<MetalCompletedOutputRouter>();
    } else {
      router_.reset();
    }
    if (graph.preview) {
      preview_ = std::make_shared<MetalPreview>(
          context_, asset.encoded_width, asset.encoded_height, *metrics_,
          [this] { stop_main_loop(); }, config.preview_visible);
      const std::weak_ptr<MetalPreview> weak_preview = preview_;
      router_->add_sink([weak_preview](MetalOutputLease output) {
        if (auto preview = weak_preview.lock()) {
          static_cast<void>(preview->offer(std::move(output)));
        }
      });
    } else {
      preview_.reset();
    }
    if (graph.encode) {
      encoder_ = std::make_shared<MetalEncoder>(
          asset.encoded_width, asset.encoded_height, config, *metrics_);
      const std::weak_ptr<MetalEncoder> weak_encoder = encoder_;
      const auto fps_num = config.fps_num;
      const auto fps_den = config.fps_den;
      router_->add_sink(
          [weak_encoder, sequence = std::uint64_t{0}, fps_num, fps_den]
          (MetalOutputLease output) mutable {
            const CMTime pts = CMTimeMake(
                static_cast<std::int64_t>(sequence++) * fps_den,
                static_cast<std::int32_t>(fps_num));
            if (auto encoder = weak_encoder.lock()) {
              static_cast<void>(encoder->offer(std::move(output), pts));
            }
          });
    } else {
      encoder_.reset();
    }
    return std::make_unique<MetalRendererAdapter>(
        context_, asset, config, *metrics_, router_, preview_, encoder_);
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

  swim::core::BackendRuntimeSample sample_runtime() const noexcept override {
    return {static_cast<std::uint64_t>(context_->device.currentAllocatedSize)};
  }

 private:
  std::shared_ptr<MetalContext> context_;
  std::shared_ptr<MetalCompletedOutputRouter> router_;
  std::shared_ptr<MetalPreview> preview_;
  std::shared_ptr<MetalEncoder> encoder_;
  swim::core::AppConfig config_;
  swim::core::RuntimeCounters fallback_metrics_;
  swim::core::RuntimeCounters* metrics_{&fallback_metrics_};
  swim::core::RunLifecycle* lifecycle_{};
  bool config_ready_{};
  std::mutex loop_mutex_;
  std::condition_variable_any loop_condition_;
  bool loop_stopped_{};
};

std::unique_ptr<swim::core::IBackend> make_metal_backend() {
  return std::make_unique<MetalBackend>();
}

}  // namespace

void register_metal_backend() {
  static std::once_flag once;
  std::call_once(once, [] {
    swim::core::BackendRegistry::instance().register_factory(
        "metal", &make_metal_backend);
  });
}

}  // namespace swim::metal
