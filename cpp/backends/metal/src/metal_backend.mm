#include <swim/metal/metal_backend.hpp>

#include <swim/core/backend.hpp>
#include <swim/metal/metal_renderer.hpp>
#include <swim/metal/mp4_source.hpp>

#import <CoreVideo/CoreVideo.h>
#import <Metal/Metal.h>

#include <array>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <utility>

namespace swim::metal {
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

class MetalRendererAdapter final : public swim::core::IRenderer {
 public:
  MetalRendererAdapter(std::shared_ptr<MetalContext> context,
                       const swim::core::RuntimeAsset& asset,
                       const swim::core::AppConfig& config,
                       swim::core::RuntimeCounters& metrics)
      : context_(std::move(context)),
        renderer_(context_, asset, config, &metrics) {
    auto* descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                    width:2
                                   height:2
                                mipmapped:NO];
    descriptor.storageMode = MTLStorageModeShared;
    descriptor.usage = MTLTextureUsageShaderRead;
    replacement_texture_ = [context_->device newTextureWithDescriptor:descriptor];
    if (replacement_texture_ == nil) {
      throw std::runtime_error("cannot create Metal replacement texture");
    }
    constexpr std::array<std::uint32_t, 4> black{};
    [replacement_texture_ replaceRegion:MTLRegionMake2D(0, 0, 2, 2)
                            mipmapLevel:0
                              withBytes:black.data()
                            bytesPerRow:2 * sizeof(std::uint32_t)];
    for (std::uint32_t camera = 0; camera < replacements_.size(); ++camera) {
      replacements_[camera].rgba = replacement_texture_;
      replacements_[camera].metadata.camera_index = camera;
      replacements_[camera].metadata.width = 2;
      replacements_[camera].metadata.height = 2;
      replacements_[camera].metadata.pixel_format = swim::core::PixelFormat::bgra8;
    }
  }

  swim::core::RenderSubmitResult submit(
      const swim::core::RenderSnapshot& snapshot) override {
    for (std::size_t camera = 0; camera < snapshot.frames.size(); ++camera) {
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
    if (camera_index >= replacements_.size()) {
      return {};
    }
    return swim::core::FrameLease{
        const_cast<MetalFrameView*>(&replacements_[camera_index]),
        {retain_static_frame, release_static_frame, kMetalFrameBackendTag},
        replacements_[camera_index].metadata};
  }

  void drain() override { renderer_.drain(); }
  bool has_fatal_error() const noexcept override {
    return renderer_.has_fatal_error();
  }
  std::string last_error() const override {
    return renderer_.fatal_error_message();
  }

 private:
  std::shared_ptr<MetalContext> context_;
  MetalStitchRenderer renderer_;
  id<MTLTexture> replacement_texture_ = nil;
  std::array<MetalFrameView, 6> replacements_;
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
        config_.decode_ticket_pool, config_.decode_surface_pool, &lifecycle_);
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
      const swim::core::AppConfig& config) override {
    config_ = config;
    config_ready_ = true;
    return std::make_unique<MetalRendererAdapter>(context_, asset, config,
                                                   *metrics_);
  }

  void run_main_loop(std::stop_token token) override {
    std::unique_lock lock(loop_mutex_);
    loop_condition_.wait(lock, token, [this] { return loop_stopped_; });
  }

  void stop_main_loop() noexcept override {
    {
      std::lock_guard lock(loop_mutex_);
      loop_stopped_ = true;
    }
    loop_condition_.notify_all();
  }

 private:
  std::shared_ptr<MetalContext> context_;
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
