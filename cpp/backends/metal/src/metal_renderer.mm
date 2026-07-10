#include <swim/metal/metal_renderer.hpp>
#include <swim/metal/videotoolbox_decoder.hpp>
#include <swim/core/render_completion_gate.hpp>

#import <CoreVideo/CoreVideo.h>
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <dispatch/dispatch.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <exception>
#include <fstream>
#include <iterator>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace swim::metal {

MetalContext::~MetalContext() {
  if (texture_cache != nullptr) {
    CFRelease(texture_cache);
    texture_cache = nullptr;
  }
}

MetalOutputLease::MetalOutputLease(MetalOutputSlot* slot) noexcept
    : slot_(slot) {}

MetalOutputLease::MetalOutputLease(const MetalOutputLease& other) noexcept
    : slot_(other.slot_), lifetime_anchor_(other.lifetime_anchor_) {
  if (slot_ != nullptr) {
    slot_->references.fetch_add(1, std::memory_order_relaxed);
  }
}

MetalOutputLease& MetalOutputLease::operator=(
    const MetalOutputLease& other) noexcept {
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

MetalOutputLease::MetalOutputLease(MetalOutputLease&& other) noexcept
    : slot_(std::exchange(other.slot_, nullptr)),
      lifetime_anchor_(std::move(other.lifetime_anchor_)) {}

MetalOutputLease& MetalOutputLease::operator=(
    MetalOutputLease&& other) noexcept {
  if (this == &other) {
    return *this;
  }
  reset();
  slot_ = std::exchange(other.slot_, nullptr);
  lifetime_anchor_ = std::move(other.lifetime_anchor_);
  return *this;
}

MetalOutputLease::~MetalOutputLease() { reset(); }

CVPixelBufferRef MetalOutputLease::pixel_buffer() const noexcept {
  return slot_ == nullptr ? nullptr : slot_->pixel_buffer;
}

id<MTLTexture> MetalOutputLease::texture() const noexcept {
  return slot_ == nullptr ? nil : slot_->texture;
}

void MetalOutputLease::anchor_lifetime(std::shared_ptr<void> owner) noexcept {
  if (lifetime_anchor_ == nullptr) {
    lifetime_anchor_ = std::move(owner);
  }
}

void MetalOutputLease::reset() noexcept {
  if (slot_ != nullptr) {
    auto* slot = std::exchange(slot_, nullptr);
    slot->owner->release(slot);
  }
  lifetime_anchor_.reset();
}

MetalOutputPool::MetalOutputPool(std::shared_ptr<MetalContext> context,
                                 std::uint32_t capacity,
                                 std::uint32_t width,
                                 std::uint32_t height)
    : context_(std::move(context)),
      capacity_(capacity),
      slots_(capacity == 0 ? nullptr
                           : std::make_unique<MetalOutputSlot[]>(capacity)) {
  if (context_ == nullptr || context_->device == nil ||
      context_->texture_cache == nullptr) {
    throw std::invalid_argument("Metal output pool requires a valid context");
  }
  if (capacity_ == 0 || capacity_ > 64) {
    throw std::invalid_argument(
        "Metal output pool capacity must be between 1 and 64");
  }
  if (width == 0 || height == 0) {
    throw std::invalid_argument("Metal output dimensions must be nonzero");
  }

  NSDictionary* attributes = @{
    (__bridge NSString*)kCVPixelBufferMetalCompatibilityKey : @YES,
    (__bridge NSString*)kCVPixelBufferIOSurfacePropertiesKey : @{},
    (__bridge NSString*)kCVPixelBufferBytesPerRowAlignmentKey : @64,
  };
  try {
    for (std::uint32_t index = 0; index < capacity_; ++index) {
      auto& slot = slots_[index];
      slot.pool_index = index;
      slot.owner = this;
      auto status = CVPixelBufferCreate(
          kCFAllocatorDefault, width, height, kCVPixelFormatType_32BGRA,
          (__bridge CFDictionaryRef)attributes, &slot.pixel_buffer);
      if (status != kCVReturnSuccess || slot.pixel_buffer == nullptr) {
        throw std::runtime_error(
            "cannot allocate IOSurface output pixel buffer");
      }
      status = CVMetalTextureCacheCreateTextureFromImage(
          kCFAllocatorDefault, context_->texture_cache, slot.pixel_buffer,
          nullptr, MTLPixelFormatBGRA8Unorm, width, height, 0,
          &slot.texture_ref);
      if (status != kCVReturnSuccess || slot.texture_ref == nullptr) {
        throw std::runtime_error("cannot create Metal output texture");
      }
      slot.texture = CVMetalTextureGetTexture(slot.texture_ref);
      if (slot.texture == nil) {
        throw std::runtime_error("Metal output texture is unavailable");
      }
    }
  } catch (...) {
    for (std::uint32_t index = 0; index < capacity_; ++index) {
      auto& slot = slots_[index];
      slot.texture = nil;
      if (slot.texture_ref != nullptr) {
        CFRelease(slot.texture_ref);
        slot.texture_ref = nullptr;
      }
      if (slot.pixel_buffer != nullptr) {
        CFRelease(slot.pixel_buffer);
        slot.pixel_buffer = nullptr;
      }
    }
    throw;
  }
}

MetalOutputPool::~MetalOutputPool() noexcept {
  for (std::uint32_t index = 0; index < capacity_; ++index) {
    if (slots_[index].references.load(std::memory_order_acquire) != 0) {
      std::terminate();
    }
  }
  for (std::uint32_t index = 0; index < capacity_; ++index) {
    auto& slot = slots_[index];
    slot.texture = nil;
    if (slot.texture_ref != nullptr) {
      CFRelease(slot.texture_ref);
      slot.texture_ref = nullptr;
    }
    if (slot.pixel_buffer != nullptr) {
      CFRelease(slot.pixel_buffer);
      slot.pixel_buffer = nullptr;
    }
  }
}

std::optional<MetalOutputLease> MetalOutputPool::try_acquire() noexcept {
  for (std::uint32_t index = 0; index < capacity_; ++index) {
    auto& slot = slots_[index];
    std::uint32_t expected = 0;
    if (!slot.references.compare_exchange_strong(
            expected, 1, std::memory_order_acquire,
            std::memory_order_relaxed)) {
      continue;
    }
    const auto usage = in_use_.fetch_add(1, std::memory_order_relaxed) + 1;
    auto previous_high = high_water_.load(std::memory_order_relaxed);
    while (usage > previous_high &&
           !high_water_.compare_exchange_weak(
               previous_high, usage, std::memory_order_relaxed,
               std::memory_order_relaxed)) {
    }
    MetalOutputLease lease{&slot};
    return std::optional<MetalOutputLease>{std::move(lease)};
  }
  return std::nullopt;
}

std::uint32_t MetalOutputPool::high_water() const noexcept {
  return high_water_.load(std::memory_order_relaxed);
}

void MetalOutputPool::release(MetalOutputSlot* slot) noexcept {
  const auto previous =
      slot->references.fetch_sub(1, std::memory_order_release);
  if (previous == 0) {
    std::terminate();
  }
  if (previous == 1) {
    in_use_.fetch_sub(1, std::memory_order_relaxed);
  }
}

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

std::string metal_error(NSString* prefix, NSError* error) {
  const char* detail = error == nil ? nullptr : error.localizedDescription.UTF8String;
  return std::string(prefix.UTF8String) +
         (detail == nullptr ? std::string{} : ": " + std::string(detail));
}

std::string read_shader_source() {
#ifndef SWIM_METAL_SHADER_SOURCE_PATH
#error "SWIM_METAL_SHADER_SOURCE_PATH must name stitch.metal"
#endif
  std::ifstream input(SWIM_METAL_SHADER_SOURCE_PATH, std::ios::binary);
  if (!input) {
    throw std::runtime_error("cannot open Metal shader source: "
                             SWIM_METAL_SHADER_SOURCE_PATH);
  }
  return {std::istreambuf_iterator<char>{input},
          std::istreambuf_iterator<char>{}};
}

id<MTLLibrary> compile_shader_library(id<MTLDevice> device) {
  const auto source = read_shader_source();
  auto* source_string = [[NSString alloc]
      initWithBytes:source.data()
             length:source.size()
           encoding:NSUTF8StringEncoding];
  if (source_string == nil) {
    throw std::runtime_error("Metal shader source is not UTF-8");
  }
  auto* options = [[MTLCompileOptions alloc] init];
  options.mathMode = MTLMathModeSafe;
  NSError* error = nil;
  id<MTLLibrary> library = [device newLibraryWithSource:source_string
                                               options:options
                                                 error:&error];
  if (library == nil) {
    throw std::runtime_error(metal_error(@"cannot compile Metal shader", error));
  }
  return library;
}

id<MTLRenderPipelineState> make_pipeline(id<MTLDevice> device,
                                        id<MTLLibrary> library,
                                        NSString* fragment_name,
                                        MTLPixelFormat pixel_format,
                                        bool additive) {
  id<MTLFunction> vertex = [library newFunctionWithName:@"stitch_vertex"];
  id<MTLFunction> fragment = [library newFunctionWithName:fragment_name];
  if (vertex == nil || fragment == nil) {
    throw std::runtime_error("Metal shader entry point is missing");
  }
  auto* descriptor = [[MTLRenderPipelineDescriptor alloc] init];
  descriptor.label = fragment_name;
  descriptor.vertexFunction = vertex;
  descriptor.fragmentFunction = fragment;
  auto* attachment = descriptor.colorAttachments[0];
  attachment.pixelFormat = pixel_format;
  attachment.blendingEnabled = additive;
  if (additive) {
    attachment.rgbBlendOperation = MTLBlendOperationAdd;
    attachment.alphaBlendOperation = MTLBlendOperationAdd;
    attachment.sourceRGBBlendFactor = MTLBlendFactorOne;
    attachment.destinationRGBBlendFactor = MTLBlendFactorOne;
    attachment.sourceAlphaBlendFactor = MTLBlendFactorOne;
    attachment.destinationAlphaBlendFactor = MTLBlendFactorOne;
  }
  NSError* error = nil;
  id<MTLRenderPipelineState> pipeline =
      [device newRenderPipelineStateWithDescriptor:descriptor error:&error];
  if (pipeline == nil) {
    throw std::runtime_error(metal_error(@"cannot create Metal pipeline", error));
  }
  return pipeline;
}

std::uint32_t validate_inflight_capacity(std::uint32_t capacity) {
  if (capacity == 0 || capacity > 64) {
    throw std::invalid_argument(
        "Metal render in-flight capacity must be between 1 and 64");
  }
  return capacity;
}

std::uint64_t seconds_to_nanoseconds(double seconds) noexcept {
  if (!(seconds > 0.0)) {
    return 0;
  }
  constexpr double kNanosecondsPerSecond = 1'000'000'000.0;
  const auto nanoseconds = seconds * kNanosecondsPerSecond;
  if (nanoseconds >=
      static_cast<double>(std::numeric_limits<std::uint64_t>::max())) {
    return std::numeric_limits<std::uint64_t>::max();
  }
  return static_cast<std::uint64_t>(nanoseconds);
}

}  // namespace

class MetalCompletedOutputRouter::Impl final
    : public std::enable_shared_from_this<MetalCompletedOutputRouter::Impl> {
 public:
  static constexpr auto kFlushTimeout = std::chrono::seconds{5};

  Impl()
      : queue_(dispatch_queue_create("swim.metal.completed-output",
                                     DISPATCH_QUEUE_SERIAL)) {
    if (queue_ == nullptr) {
      throw std::runtime_error("cannot create Metal completed-output queue");
    }
  }

  void add_sink(MetalCompletedOutputSink sink) {
    if (!sink) {
      throw std::invalid_argument("completed-output sink must be callable");
    }
    if (!accepting_.load(std::memory_order_acquire)) {
      throw std::logic_error("completed-output router is closed");
    }
    sinks_.push_back(std::move(sink));
  }

  bool route(MetalOutputLease output) noexcept {
    if (!delivery_gate_.try_accept()) {
      return false;
    }
    auto owner = shared_from_this();
    MetalOutputLease delivery = std::move(output);
    dispatch_async(queue_, ^{
      @autoreleasepool {
        owner->deliver(delivery);
        owner->delivery_gate_.complete();
      }
    });
    return true;
  }

  void close_and_flush() {
    if (!delivery_gate_.close_and_wait_until(
            std::chrono::steady_clock::now() + kFlushTimeout)) {
      throw std::runtime_error(
          "timed out flushing Metal completed-output router");
    }
    accepting_.store(false, std::memory_order_release);
  }

 private:
  void deliver(MetalOutputLease output) noexcept {
    for (std::size_t index = 0; index < sinks_.size(); ++index) {
      try {
        if (index + 1 == sinks_.size()) {
          sinks_[index](std::move(output));
        } else {
          sinks_[index](output);
        }
      } catch (...) {
        // A downstream consumer cannot unwind onto the dispatch queue or
        // prevent other independently registered consumers from receiving.
      }
    }
  }

  dispatch_queue_t queue_;
  std::vector<MetalCompletedOutputSink> sinks_;
  std::atomic_bool accepting_{true};
  swim::core::RenderCompletionGate delivery_gate_;
};

MetalCompletedOutputRouter::MetalCompletedOutputRouter()
    : impl_(std::make_shared<Impl>()) {}

MetalCompletedOutputRouter::~MetalCompletedOutputRouter() {
  if (impl_ != nullptr) {
    try {
      impl_->close_and_flush();
    } catch (...) {
    }
  }
}

void MetalCompletedOutputRouter::add_sink(MetalCompletedOutputSink sink) {
  impl_->add_sink(std::move(sink));
}

bool MetalCompletedOutputRouter::route(MetalOutputLease output) noexcept {
  return impl_->route(std::move(output));
}

void MetalCompletedOutputRouter::close_and_flush() {
  impl_->close_and_flush();
}

class MetalStitchRenderer::Impl final
    : public std::enable_shared_from_this<MetalStitchRenderer::Impl> {
 public:
  friend class MetalStitchRenderer;
  static constexpr auto kDrainTimeout = std::chrono::seconds{5};
  struct CameraResources final {
    id<MTLBuffer> vertices = nil;
    id<MTLBuffer> indices = nil;
    id<MTLTexture> weights = nil;
    NSUInteger index_count = 0;
    NSUInteger vertex_bytes = 0;
    float weight_x = 0.0F;
    float weight_y = 0.0F;
    float weight_width = 0.0F;
    float weight_height = 0.0F;
    float mesh_min_x = 0.0F;
    float mesh_min_y = 0.0F;
    float mesh_max_x = 0.0F;
    float mesh_max_y = 0.0F;
  };

  struct InFlightRecord final {
    std::atomic_bool busy{false};
    id<MTLTexture> accumulation = nil;
    std::array<MetalFrameView, 6> frame_views;
    std::array<swim::core::FrameLease, 6> input_leases;
    MetalOutputLease output;
  };

  Impl(std::shared_ptr<MetalContext> context,
       const swim::core::RuntimeAsset& asset,
       const swim::core::AppConfig& config,
       swim::core::RuntimeCounters* metrics,
       MetalCompletedOutputSink completed_output_sink)
      : context_(std::move(context)),
        logical_width_(asset.logical_width),
        logical_height_(asset.logical_height),
        encoded_width_(asset.encoded_width),
        encoded_height_(asset.encoded_height),
        inflight_count_(validate_inflight_capacity(config.render_inflight)),
        in_flight_(std::make_unique<InFlightRecord[]>(inflight_count_)),
        output_pool_(context_, config.output_pool, encoded_width_,
                     encoded_height_),
        metrics_(metrics),
        completed_output_sink_(std::move(completed_output_sink)) {
    if (context_ == nullptr || context_->device == nil ||
        context_->command_queue == nil || context_->texture_cache == nullptr) {
      throw std::invalid_argument("Metal renderer requires a valid context");
    }
    if (logical_width_ == 0 || logical_height_ == 0 ||
        encoded_width_ < logical_width_ || encoded_height_ < logical_height_) {
      throw std::invalid_argument("Metal renderer dimensions are invalid");
    }
    if (asset.cameras.size() != cameras_.size()) {
      throw std::invalid_argument("Metal renderer requires six cameras");
    }
    if (metrics_ != nullptr) {
      metrics_->render_inflight_capacity.store(inflight_count_,
                                               std::memory_order_relaxed);
      metrics_->render_output_capacity.store(config.output_pool,
                                             std::memory_order_relaxed);
    }
    for (std::size_t index = 0; index < cameras_.size(); ++index) {
      if (asset.cameras[index].camera_id != kCameraOrder[index]) {
        throw std::invalid_argument(
            "Metal renderer camera order must be cam3,cam2,cam1,cam4,cam5,cam6");
      }
    }

    library_ = compile_shader_library(context_->device);
    rgba_pipeline_ = make_pipeline(context_->device, library_, @"stitch_rgba",
                                   MTLPixelFormatRGBA16Float, true);
    nv12_pipeline_ = make_pipeline(context_->device, library_, @"stitch_nv12",
                                   MTLPixelFormatRGBA16Float, true);
    resolve_pipeline_ = make_pipeline(context_->device, library_,
                                      @"resolve_accumulation",
                                      MTLPixelFormatBGRA8Unorm, false);

    upload_camera_resources(asset);
    upload_fullscreen_triangle();
    allocate_accumulation_textures();
  }

  ~Impl() { drain_noexcept(); }

  bool submit(const std::array<MetalFrameView, 6>& frames,
              const std::array<swim::core::FrameLease, 6>* leases,
              MetalRenderResult& result) noexcept {
    result.output = {};
    result.gpu_start_ns = 0;
    result.gpu_end_ns = 0;
    result.diagnostic_command_buffer = nil;
    if (fatal_error_.load(std::memory_order_acquire)) {
      return false;
    }
    for (std::size_t index = 0; index < frames.size(); ++index) {
      if (frames[index].metadata.camera_index != index) {
        return false;
      }
    }
    if (!completion_gate_.try_accept()) {
      return false;
    }

    InFlightRecord* record = nullptr;
    for (std::uint32_t index = 0; index < inflight_count_; ++index) {
      bool expected = false;
      if (in_flight_[index].busy.compare_exchange_strong(
              expected, true, std::memory_order_acquire,
              std::memory_order_relaxed)) {
        record = &in_flight_[index];
        break;
      }
    }
    if (record == nullptr) {
      record_pool_miss(true);
      completion_gate_.complete();
      return false;
    }
    const auto in_use =
        inflight_in_use_.fetch_add(1, std::memory_order_relaxed) + 1;
    update_high_water(inflight_high_water_, in_use,
                      metrics_ == nullptr
                          ? nullptr
                          : &metrics_->render_inflight_high_water);

    auto output = output_pool_.try_acquire();
    if (!output) {
      inflight_in_use_.fetch_sub(1, std::memory_order_relaxed);
      record->busy.store(false, std::memory_order_release);
      record_pool_miss(false);
      completion_gate_.complete();
      return false;
    }
    if (metrics_ != nullptr) {
      metrics_->render_output_high_water.store(output_pool_.high_water(),
                                               std::memory_order_relaxed);
    }

    try {
      record->frame_views = frames;
      if (leases != nullptr) {
        record->input_leases = *leases;
      } else {
        record->input_leases = {};
      }
      result.output = std::move(*output);
      result.output.anchor_lifetime(shared_from_this());
      record->output = result.output;

      id<MTLCommandBuffer> command = [context_->command_queue commandBuffer];
      if (command == nil) {
        throw std::runtime_error("cannot create Metal command buffer");
      }
      if (metrics_ != nullptr) {
        metrics_->native_command_buffers.fetch_add(1,
                                                    std::memory_order_relaxed);
      }
      encode_stitch(command, frames, *record, result.output.texture());
      result.diagnostic_command_buffer = command;

      auto* completed_record = record;
      auto owner = shared_from_this();
      [command addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        MetalOutputLease completed_output;
        if (completed.status != MTLCommandBufferStatusCompleted) {
          owner->record_fatal(
              metal_error(@"Metal command buffer failed", completed.error));
        } else {
          owner->record_completion();
          completed_output = std::move(completed_record->output);
        }
        completed_record->input_leases = {};
        completed_record->frame_views = {};
        completed_record->output = {};
        owner->inflight_in_use_.fetch_sub(1, std::memory_order_relaxed);
        completed_record->busy.store(false, std::memory_order_release);
        if (completed_output && owner->completed_output_sink_) {
          try {
            owner->completed_output_sink_(std::move(completed_output));
          } catch (...) {
            owner->record_fatal("completed-output sink failed");
          }
        }
        owner->completion_gate_.complete();
      }];
      record_first_submit();
      [command commit];
      return true;
    } catch (const std::exception& error) {
      record_fatal(error.what());
      record->input_leases = {};
      record->frame_views = {};
      record->output = {};
      inflight_in_use_.fetch_sub(1, std::memory_order_relaxed);
      record->busy.store(false, std::memory_order_release);
      result.output = {};
      result.diagnostic_command_buffer = nil;
      completion_gate_.complete();
      return false;
    } catch (...) {
      record_fatal("unknown Metal command submission failure");
      record->input_leases = {};
      record->frame_views = {};
      record->output = {};
      inflight_in_use_.fetch_sub(1, std::memory_order_relaxed);
      record->busy.store(false, std::memory_order_release);
      result.output = {};
      result.diagnostic_command_buffer = nil;
      completion_gate_.complete();
      return false;
    }
  }

  void wait_for_completion(MetalRenderResult& result) {
    id<MTLCommandBuffer> command = result.diagnostic_command_buffer;
    if (command == nil) {
      throw std::invalid_argument(
          "Metal render result has no diagnostic submission");
    }
    [command waitUntilCompleted];
    if (command.status != MTLCommandBufferStatusCompleted) {
      fatal_error_.store(true, std::memory_order_release);
      throw std::runtime_error(
          metal_error(@"Metal command buffer failed", command.error));
    }
    result.gpu_start_ns = seconds_to_nanoseconds(command.GPUStartTime);
    result.gpu_end_ns = seconds_to_nanoseconds(command.GPUEndTime);
  }

  bool has_fatal_error() const noexcept {
    return fatal_error_.load(std::memory_order_acquire);
  }

  std::string fatal_error_message() const {
    std::lock_guard lock(fatal_error_mutex_);
    return fatal_error_message_;
  }

  bool uploaded_vertices_match(
      const swim::core::RuntimeAsset& asset) const noexcept {
    if (asset.cameras.size() != cameras_.size()) {
      return false;
    }
    for (std::size_t index = 0; index < cameras_.size(); ++index) {
      const auto& source = asset.cameras[index];
      const auto& camera = cameras_[index];
      const auto expected_bytes =
          source.vertices.size() * sizeof(source.vertices[0]);
      if (camera.vertices == nil || camera.vertex_bytes != expected_bytes ||
          camera.vertices.length != expected_bytes ||
          std::memcmp(camera.vertices.contents, source.vertices.data(),
                      expected_bytes) != 0) {
        return false;
      }
    }
    return true;
  }

  float raster_position_expansion() const noexcept {
    return kInclusiveExpansion;
  }

  void drain() {
    if (drain_timed_out_.load(std::memory_order_acquire)) {
      flush_completion_metrics();
      throw std::runtime_error("timed out waiting for Metal render completion");
    }
    if (!completion_gate_.close_and_wait_until(
            std::chrono::steady_clock::now() + kDrainTimeout)) {
      drain_timed_out_.store(true, std::memory_order_release);
      record_fatal("timed out waiting for Metal render completion");
      flush_completion_metrics();
      throw std::runtime_error("timed out waiting for Metal render completion");
    }
    flush_completion_metrics();
  }

 private:
  static std::uint64_t steady_nanoseconds() noexcept {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<
        std::chrono::nanoseconds>(std::chrono::steady_clock::now()
                                     .time_since_epoch())
                                          .count());
  }

  static void update_high_water(
      std::atomic_uint32_t& local, std::uint32_t usage,
      std::atomic_uint64_t* external) noexcept {
    auto high = local.load(std::memory_order_relaxed);
    while (usage > high &&
           !local.compare_exchange_weak(high, usage,
                                        std::memory_order_relaxed,
                                        std::memory_order_relaxed)) {
    }
    if (external != nullptr) {
      external->store(local.load(std::memory_order_relaxed),
                      std::memory_order_relaxed);
    }
  }

  void record_pool_miss(bool inflight) noexcept {
    if (metrics_ == nullptr) {
      return;
    }
    metrics_->pool_exhaustion.fetch_add(1, std::memory_order_relaxed);
    auto& counter = inflight ? metrics_->render_inflight_pool_misses
                             : metrics_->render_output_pool_misses;
    counter.fetch_add(1, std::memory_order_relaxed);
  }

  void record_first_submit() noexcept {
    auto expected = std::uint64_t{0};
    first_submit_ns_.compare_exchange_strong(
        expected, steady_nanoseconds(), std::memory_order_relaxed,
        std::memory_order_relaxed);
  }

  void record_completion() noexcept {
    completed_count_.fetch_add(1, std::memory_order_relaxed);
    swim::core::record_atomic_max(last_completion_ns_, steady_nanoseconds());
  }

  void flush_completion_metrics() noexcept {
    if (completion_metrics_flushed_.exchange(true,
                                             std::memory_order_acq_rel)) {
      return;
    }
    auto* metrics = std::exchange(metrics_, nullptr);
    if (metrics == nullptr) {
      return;
    }
    metrics->render_completions.fetch_add(
        completed_count_.load(std::memory_order_relaxed),
        std::memory_order_relaxed);
    auto expected = std::uint64_t{0};
    metrics->render_first_submit_ns.compare_exchange_strong(
        expected, first_submit_ns_.load(std::memory_order_relaxed),
        std::memory_order_relaxed, std::memory_order_relaxed);
    swim::core::record_atomic_max(
        metrics->render_last_completion_ns,
        last_completion_ns_.load(std::memory_order_relaxed));
  }

  void drain_noexcept() noexcept {
    try {
      drain();
    } catch (...) {
      flush_completion_metrics();
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

  void upload_camera_resources(const swim::core::RuntimeAsset& asset) {
    for (std::size_t index = 0; index < cameras_.size(); ++index) {
      const auto& source = asset.cameras[index];
      auto& camera = cameras_[index];
      if (source.vertices.empty() || source.indices.empty() ||
          source.weight_width == 0 || source.weight_height == 0 ||
          source.weights.size() !=
              static_cast<std::size_t>(source.weight_width) *
                  source.weight_height) {
        throw std::invalid_argument("Metal camera asset is empty or malformed");
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
      camera.vertices = [context_->device
          newBufferWithBytes:source.vertices.data()
                     length:source.vertices.size() * sizeof(source.vertices[0])
                    options:MTLResourceStorageModeShared];
      camera.indices = [context_->device
          newBufferWithBytes:source.indices.data()
                     length:source.indices.size() * sizeof(source.indices[0])
                    options:MTLResourceStorageModeShared];
      auto* descriptor = [MTLTextureDescriptor
          texture2DDescriptorWithPixelFormat:MTLPixelFormatR16Unorm
                                      width:source.weight_width
                                     height:source.weight_height
                                  mipmapped:NO];
      descriptor.storageMode = MTLStorageModeShared;
      descriptor.usage = MTLTextureUsageShaderRead;
      camera.weights =
          [context_->device newTextureWithDescriptor:descriptor];
      if (camera.vertices == nil || camera.indices == nil ||
          camera.weights == nil) {
        throw std::runtime_error("cannot upload static Metal camera resources");
      }
      [camera.weights
          replaceRegion:MTLRegionMake2D(0, 0, source.weight_width,
                                        source.weight_height)
            mipmapLevel:0
              withBytes:source.weights.data()
            bytesPerRow:static_cast<NSUInteger>(source.weight_width) *
                        sizeof(source.weights[0])];
      camera.index_count = source.indices.size();
      camera.vertex_bytes =
          source.vertices.size() * sizeof(source.vertices[0]);
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
    const std::array<swim::core::disk::VertexV1, 3> vertices{{
        {0.0F, 0.0F, 0.0F, 0.0F},
        {2.0F * static_cast<float>(encoded_width_), 0.0F, 0.0F, 0.0F},
        {0.0F, 2.0F * static_cast<float>(encoded_height_), 0.0F, 0.0F},
    }};
    fullscreen_vertices_ = [context_->device
        newBufferWithBytes:vertices.data()
                   length:sizeof(vertices)
                  options:MTLResourceStorageModeShared];
    if (fullscreen_vertices_ == nil) {
      throw std::runtime_error("cannot upload Metal fullscreen triangle");
    }
  }

  void allocate_accumulation_textures() {
    auto* descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA16Float
                                    width:encoded_width_
                                   height:encoded_height_
                                mipmapped:NO];
    descriptor.storageMode = MTLStorageModePrivate;
    descriptor.usage = MTLTextureUsageRenderTarget | MTLTextureUsageShaderRead;
    for (std::uint32_t index = 0; index < inflight_count_; ++index) {
      in_flight_[index].accumulation =
          [context_->device newTextureWithDescriptor:descriptor];
      if (in_flight_[index].accumulation == nil) {
        throw std::runtime_error(
            "cannot preallocate Metal accumulation texture");
      }
    }
  }

  void encode_stitch(id<MTLCommandBuffer> command,
                     const std::array<MetalFrameView, 6>& frames,
                     InFlightRecord& record,
                     id<MTLTexture> output_texture) {
    auto* accumulation_pass = [MTLRenderPassDescriptor renderPassDescriptor];
    auto* accumulation_attachment = accumulation_pass.colorAttachments[0];
    accumulation_attachment.texture = record.accumulation;
    accumulation_attachment.loadAction = MTLLoadActionClear;
    accumulation_attachment.storeAction = MTLStoreActionStore;
    accumulation_attachment.clearColor = MTLClearColorMake(0, 0, 0, 0);
    id<MTLRenderCommandEncoder> encoder =
        [command renderCommandEncoderWithDescriptor:accumulation_pass];
    if (encoder == nil) {
      throw std::runtime_error("cannot create Metal stitch encoder");
    }

    for (std::size_t index = 0; index < cameras_.size(); ++index) {
      const auto& camera = cameras_[index];
      const auto& frame = frames[index];
      const auto texture_width = frame.rgba != nil ? frame.rgba.width
                                                    : frame.luma.width;
      const auto texture_height = frame.rgba != nil ? frame.rgba.height
                                                     : frame.luma.height;
      if (texture_width == 0 || texture_height == 0) {
        [encoder endEncoding];
        throw std::invalid_argument("Metal input texture is unavailable");
      }
      const FragmentUniforms fragment_uniforms{
          1.0F / static_cast<float>(texture_width),
          1.0F / static_cast<float>(texture_height), camera.weight_x,
          camera.weight_y, camera.weight_width, camera.weight_height,
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

      [encoder setVertexBuffer:camera.vertices offset:0 atIndex:0];
      [encoder setVertexBytes:&vertex_uniforms
                       length:sizeof(vertex_uniforms)
                      atIndex:1];
      [encoder setFragmentBytes:&fragment_uniforms
                         length:sizeof(fragment_uniforms)
                        atIndex:0];
      if (frame.rgba != nil) {
        [encoder setRenderPipelineState:rgba_pipeline_];
        [encoder setFragmentTexture:frame.rgba atIndex:0];
        [encoder setFragmentTexture:camera.weights atIndex:1];
      } else {
        if (frame.luma == nil || frame.chroma == nil) {
          [encoder endEncoding];
          throw std::invalid_argument("Metal NV12 planes are unavailable");
        }
        [encoder setRenderPipelineState:nv12_pipeline_];
        [encoder setFragmentTexture:frame.luma atIndex:0];
        [encoder setFragmentTexture:frame.chroma atIndex:1];
        [encoder setFragmentTexture:camera.weights atIndex:2];
      }
      [encoder drawIndexedPrimitives:MTLPrimitiveTypeTriangle
                          indexCount:camera.index_count
                           indexType:MTLIndexTypeUInt32
                         indexBuffer:camera.indices
                   indexBufferOffset:0];
    }
    [encoder endEncoding];

    auto* resolve_pass = [MTLRenderPassDescriptor renderPassDescriptor];
    auto* resolve_attachment = resolve_pass.colorAttachments[0];
    resolve_attachment.texture = output_texture;
    resolve_attachment.loadAction = MTLLoadActionDontCare;
    resolve_attachment.storeAction = MTLStoreActionStore;
    encoder = [command renderCommandEncoderWithDescriptor:resolve_pass];
    if (encoder == nil) {
      throw std::runtime_error("cannot create Metal resolve encoder");
    }
    const VertexUniforms resolve_vertex_uniforms{
        static_cast<float>(encoded_width_),
        static_cast<float>(encoded_height_), 0.0F, 0.0F,
        0.0F, 0.0F, 0.0F, 0.0F, 0.0F, 0.0F, 0U, 0U};
    const std::array<std::uint32_t, 4> dimensions{
        logical_width_, logical_height_, encoded_width_, encoded_height_};
    [encoder setRenderPipelineState:resolve_pipeline_];
    [encoder setVertexBuffer:fullscreen_vertices_ offset:0 atIndex:0];
    [encoder setVertexBytes:&resolve_vertex_uniforms
                     length:sizeof(resolve_vertex_uniforms)
                    atIndex:1];
    [encoder setFragmentTexture:record.accumulation atIndex:0];
    [encoder setFragmentBytes:dimensions.data()
                       length:sizeof(dimensions)
                      atIndex:0];
    [encoder drawPrimitives:MTLPrimitiveTypeTriangle
                vertexStart:0
                vertexCount:3];
    [encoder endEncoding];
  }

  std::shared_ptr<MetalContext> context_;
  std::uint32_t logical_width_;
  std::uint32_t logical_height_;
  std::uint32_t encoded_width_;
  std::uint32_t encoded_height_;
  std::array<CameraResources, 6> cameras_;
  id<MTLBuffer> fullscreen_vertices_ = nil;
  id<MTLLibrary> library_ = nil;
  id<MTLRenderPipelineState> rgba_pipeline_ = nil;
  id<MTLRenderPipelineState> nv12_pipeline_ = nil;
  id<MTLRenderPipelineState> resolve_pipeline_ = nil;
  std::uint32_t inflight_count_;
  std::unique_ptr<InFlightRecord[]> in_flight_;
  MetalOutputPool output_pool_;
  swim::core::RuntimeCounters* metrics_;
  MetalCompletedOutputSink completed_output_sink_;
  std::atomic_uint32_t inflight_in_use_{0};
  std::atomic_uint32_t inflight_high_water_{0};
  swim::core::RenderCompletionGate completion_gate_;
  std::atomic_uint64_t completed_count_{0};
  std::atomic_uint64_t first_submit_ns_{0};
  std::atomic_uint64_t last_completion_ns_{0};
  std::atomic_bool completion_metrics_flushed_{false};
  std::atomic_bool drain_timed_out_{false};
  std::atomic_bool fatal_error_{false};
  mutable std::mutex fatal_error_mutex_;
  std::string fatal_error_message_;
};

MetalStitchRenderer::MetalStitchRenderer(
    std::shared_ptr<MetalContext> context,
    const swim::core::RuntimeAsset& asset,
    const swim::core::AppConfig& config,
    swim::core::RuntimeCounters* metrics,
    MetalCompletedOutputSink completed_output_sink)
    : impl_(std::make_shared<Impl>(std::move(context), asset, config,
                                   metrics,
                                   std::move(completed_output_sink))) {}

MetalStitchRenderer::~MetalStitchRenderer() {
  if (impl_ != nullptr) {
    try {
      impl_->drain();
    } catch (...) {
    }
  }
}

bool MetalStitchRenderer::submit(
    const std::array<MetalFrameView, 6>& frames,
    MetalRenderResult& result) noexcept {
  return impl_->submit(frames, nullptr, result);
}

bool MetalStitchRenderer::submit(
    const swim::core::RenderSnapshot& snapshot,
    MetalRenderResult& result) noexcept {
  try {
    std::array<MetalFrameView, 6> frames;
    for (std::size_t index = 0; index < frames.size(); ++index) {
      if (snapshot.frames[index].metadata().camera_index != index) {
        return false;
      }
      const auto& lease = snapshot.frames[index];
      switch (lease.backend_tag()) {
        case kMetalFrameBackendTag: {
          auto* native = static_cast<MetalFrameView*>(
              lease.native(kMetalFrameBackendTag));
          if (native == nullptr) {
            return false;
          }
          frames[index] = *native;
          break;
        }
        case kMetalDecodedSurfaceTag: {
          auto* decoded = static_cast<MetalDecodedSurface*>(
              lease.native(kMetalDecodedSurfaceTag));
          if (decoded == nullptr) {
            return false;
          }
          frames[index] = decoded->view;
          break;
        }
        default:
          return false;
      }
      frames[index].metadata = snapshot.frames[index].metadata();
    }
    return impl_->submit(frames, &snapshot.frames, result);
  } catch (...) {
    return false;
  }
}

void MetalStitchRenderer::wait_for_completion(MetalRenderResult& result) {
  impl_->wait_for_completion(result);
}

void MetalStitchRenderer::drain() { impl_->drain(); }

bool MetalStitchRenderer::has_fatal_error() const noexcept {
  return impl_->has_fatal_error();
}

std::string MetalStitchRenderer::fatal_error_message() const {
  return impl_->fatal_error_message();
}

bool MetalStitchRenderer::uploaded_vertices_match(
    const swim::core::RuntimeAsset& asset) const noexcept {
  return impl_->uploaded_vertices_match(asset);
}

float MetalStitchRenderer::raster_position_expansion() const noexcept {
  return impl_->raster_position_expansion();
}

}  // namespace swim::metal
