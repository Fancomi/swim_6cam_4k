#include <swim/cudagl/cudagl_backend.hpp>

#include <swim/core/backend.hpp>
#include <swim/core/benchmark_stage.hpp>
#include <swim/cudagl/cudagl_frame.hpp>
#include <swim/cudagl/cudagl_preview.hpp>
#include <swim/cudagl/cudagl_renderer.hpp>
#include <swim/cudagl/nvdec_source.hpp>

#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include <cuda_runtime.h>

#include <array>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>

namespace swim::cudagl {

CudaGlContext::~CudaGlContext() {
  if (gl_context != nullptr) {
    glfwDestroyWindow(gl_context);
    gl_context = nullptr;
  }
  if (glfw_owned) {
    glfwTerminate();
  }
}

namespace {

std::shared_ptr<CudaGlContext> make_context() {
  auto ctx = std::make_shared<CudaGlContext>();
  if (glfwInit() == GLFW_FALSE) {
    throw std::runtime_error("glfwInit failed");
  }
  ctx->glfw_owned = true;
  // Hidden base window owns the shared GL context. Created on the main thread;
  // made current on the render thread by the renderer.
  glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
  glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 3);
  glfwWindowHint(GLFW_OPENGL_PROFILE, GLFW_OPENGL_CORE_PROFILE);
  glfwWindowHint(GLFW_VISIBLE, GLFW_FALSE);
  ctx->gl_context = glfwCreateWindow(16, 16, "swim-gl-base", nullptr, nullptr);
  if (ctx->gl_context == nullptr) {
    throw std::runtime_error("cannot create base GL context");
  }
  int device_count = 0;
  if (cudaGetDeviceCount(&device_count) != cudaSuccess || device_count == 0) {
    throw std::runtime_error("no CUDA device available");
  }
  ctx->cuda_device = 0;
  return ctx;
}

class CudaGlRendererAdapter final : public swim::core::IRenderer {
 public:
  CudaGlRendererAdapter(std::shared_ptr<CudaGlContext> context,
                        const swim::core::RuntimeAsset& asset,
                        const swim::core::AppConfig& config,
                        swim::core::RuntimeCounters& metrics,
                        std::shared_ptr<CudaGlPreview> preview)
      : preview_(std::move(preview)),
        renderer_(context, asset, config, &metrics, make_sink(preview_)) {}

  swim::core::RenderSubmitResult submit(
      const swim::core::RenderSnapshot& snapshot) override {
    for (std::size_t camera = 0; camera < snapshot.camera_count; ++camera) {
      if (!snapshot.frames[camera]) {
        return swim::core::RenderSubmitResult::not_ready;
      }
      if (snapshot.frames[camera].metadata().camera_index != camera) {
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

  // NVDEC-only path has no synthetic placeholder frames; render-only benchmark
  // is not supported on this backend, so replacement/benchmark leases are empty.
  swim::core::FrameLease replacement_frame(std::uint32_t) const override {
    return {};
  }
  swim::core::FrameLease benchmark_frame(std::uint32_t) const override {
    return {};
  }

  void drain() override { renderer_.drain(); }
  bool has_fatal_error() const noexcept override {
    return renderer_.has_fatal_error();
  }
  std::string last_error() const override {
    return renderer_.fatal_error_message();
  }

 private:
  static CudaGlCompletedOutputSink make_sink(
      const std::shared_ptr<CudaGlPreview>& preview) {
    if (preview == nullptr) {
      return {};
    }
    std::weak_ptr<CudaGlPreview> weak = preview;
    return [weak](GLuint texture) {
      if (auto locked = weak.lock()) {
        locked->offer(texture);
      }
    };
  }

  std::shared_ptr<CudaGlPreview> preview_;
  CudaGlStitchRenderer renderer_;
};

class CudaGlSourceAdapter final : public swim::core::ISource {
 public:
  CudaGlSourceAdapter(std::shared_ptr<CudaGlContext> context,
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
    source_impl_ = std::make_unique<NvdecSource>(
        context_, source_, camera_index_, output, metrics_, config_.mode,
        config_.decode_surface_pool, &lifecycle_, config_.loop_sources,
        config_.stop_at_eof, config_.loop_period, &shared_origin_);
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
  std::shared_ptr<CudaGlContext> context_;
  swim::core::SourceConfig source_;
  std::uint32_t camera_index_;
  swim::core::AppConfig config_;
  swim::core::RuntimeCounters& metrics_;
  swim::core::RunLifecycle& lifecycle_;
  swim::core::SharedLaneOrigin& shared_origin_;
  std::unique_ptr<NvdecSource> source_impl_;
};

class CudaGlBackend final : public swim::core::IBackend {
 public:
  CudaGlBackend() : context_(make_context()) {}

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
      throw std::logic_error("CUDA/GL renderer must be created before sources");
    }
    if (lifecycle_ == nullptr) {
      throw std::logic_error("CUDA/GL lifecycle must be bound before sources");
    }
    return std::make_unique<CudaGlSourceAdapter>(
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
    if (graph.synthetic_inputs) {
      throw std::runtime_error(
          "CUDA/GL backend does not support synthetic render-only input");
    }
    if (graph.preview) {
      preview_ = std::make_shared<CudaGlPreview>(
          context_, asset.encoded_width, asset.encoded_height, *metrics_,
          [this] { stop_main_loop(); }, config.preview_visible);
    } else {
      preview_.reset();
    }
    return std::make_unique<CudaGlRendererAdapter>(context_, asset, config,
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
  std::shared_ptr<CudaGlContext> context_;
  std::shared_ptr<CudaGlPreview> preview_;
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

std::unique_ptr<swim::core::IBackend> make_cudagl_backend() {
  return std::make_unique<CudaGlBackend>();
}

}  // namespace

void register_cudagl_backend() {
  static std::once_flag once;
  std::call_once(once, [] {
    swim::core::BackendRegistry::instance().register_factory(
        "cudagl", &make_cudagl_backend);
  });
}

}  // namespace swim::cudagl
