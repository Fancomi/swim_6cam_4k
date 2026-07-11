#include <swim/metal/metal_preview.hpp>

#include <swim/core/render_completion_gate.hpp>

#import <Cocoa/Cocoa.h>
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>

@interface SwimPreviewDelegate : NSObject <NSWindowDelegate>
@property(nonatomic, copy) dispatch_block_t closeHandler;
@end

@implementation SwimPreviewDelegate
- (void)windowWillClose:(NSNotification*)notification {
  static_cast<void>(notification);
  if (self.closeHandler != nil) {
    self.closeHandler();
  }
}
@end

@interface SwimPreviewTimerTarget : NSObject
@property(nonatomic, copy) dispatch_block_t tickHandler;
- (void)tick:(NSTimer*)timer;
@end

@implementation SwimPreviewTimerTarget
- (void)tick:(NSTimer*)timer {
  static_cast<void>(timer);
  if (self.tickHandler != nil) {
    self.tickHandler();
  }
}
@end

namespace swim::metal {
namespace {

struct PreviewScale final {
  float x;
  float y;
};

static_assert(sizeof(PreviewScale) == 2 * sizeof(float));

void require_main_thread(const char* operation) {
  if (![NSThread isMainThread]) {
    throw std::logic_error(std::string(operation) +
                           " must run on the process main thread");
  }
}

std::runtime_error metal_error(NSString* operation, NSError* error) {
  const char* detail =
      error == nil ? nullptr : error.localizedDescription.UTF8String;
  return std::runtime_error(
      std::string(operation.UTF8String) +
      (detail == nullptr ? std::string{} : ": " + std::string(detail)));
}

id<MTLRenderPipelineState> make_preview_pipeline(id<MTLDevice> device) {
  static NSString* const source = @R"metal(
#include <metal_stdlib>
using namespace metal;

struct PreviewVertexOut {
  float4 position [[position]];
  float2 uv;
};

vertex PreviewVertexOut preview_vertex(uint vertex_id [[vertex_id]],
                                        constant float2& scale [[buffer(0)]]) {
  constexpr float2 positions[3] = {
      float2(-1.0, -1.0), float2(3.0, -1.0), float2(-1.0, 3.0)};
  constexpr float2 uvs[3] = {
      float2(0.0, 1.0), float2(2.0, 1.0), float2(0.0, -1.0)};
  PreviewVertexOut out;
  out.position = float4(positions[vertex_id] * scale, 0.0, 1.0);
  out.uv = uvs[vertex_id];
  return out;
}

fragment float4 preview_fragment(PreviewVertexOut in [[stage_in]],
                                 texture2d<float> source [[texture(0)]],
                                 sampler linear_sampler [[sampler(0)]]) {
  return source.sample(linear_sampler, in.uv);
}
)metal";

  NSError* error = nil;
  id<MTLLibrary> library = [device newLibraryWithSource:source
                                                options:nil
                                                  error:&error];
  if (library == nil) {
    throw metal_error(@"cannot compile Metal preview shader", error);
  }
  id<MTLFunction> vertex = [library newFunctionWithName:@"preview_vertex"];
  id<MTLFunction> fragment =
      [library newFunctionWithName:@"preview_fragment"];
  if (vertex == nil || fragment == nil) {
    throw std::runtime_error("Metal preview shader entry point is missing");
  }
  auto* descriptor = [[MTLRenderPipelineDescriptor alloc] init];
  descriptor.label = @"swim.preview.pipeline";
  descriptor.vertexFunction = vertex;
  descriptor.fragmentFunction = fragment;
  descriptor.colorAttachments[0].pixelFormat = MTLPixelFormatBGRA8Unorm;
  id<MTLRenderPipelineState> pipeline =
      [device newRenderPipelineStateWithDescriptor:descriptor error:&error];
  if (pipeline == nil) {
    throw metal_error(@"cannot create Metal preview pipeline", error);
  }
  return pipeline;
}

}  // namespace

class MetalPreview::Impl final
    : public std::enable_shared_from_this<MetalPreview::Impl> {
 public:
  static constexpr auto kDrainTimeout = std::chrono::seconds{5};

  Impl(std::shared_ptr<MetalContext> context, std::uint32_t width,
       std::uint32_t height, swim::core::RuntimeCounters& metrics,
       CloseCallback close_callback)
      : context_(std::move(context)),
        source_width_(width),
        source_height_(height),
        metrics_(&metrics),
        close_callback_(std::move(close_callback)) {
    if (context_ == nullptr || context_->device == nil ||
        context_->command_queue == nil || width == 0 || height == 0) {
      throw std::invalid_argument("Metal preview requires a valid context and dimensions");
    }
  }

  void initialize() {
    require_main_thread("Metal preview initialization");
    @autoreleasepool {
      application_ = [NSApplication sharedApplication];
      [application_ setActivationPolicy:NSApplicationActivationPolicyRegular];

      constexpr CGFloat initial_width = 1280.0;
      const auto aspect = static_cast<CGFloat>(source_height_) /
                          static_cast<CGFloat>(source_width_);
      const NSRect frame = NSMakeRect(80.0, 80.0, initial_width,
                                      std::max<CGFloat>(360.0,
                                                        initial_width * aspect));
      window_ = [[NSWindow alloc]
          initWithContentRect:frame
                    styleMask:(NSWindowStyleMaskTitled |
                               NSWindowStyleMaskClosable |
                               NSWindowStyleMaskResizable |
                               NSWindowStyleMaskMiniaturizable)
                      backing:NSBackingStoreBuffered
                        defer:NO];
      if (window_ == nil) {
        throw std::runtime_error("cannot create Metal preview window");
      }
      window_.title = @"Swimming 6-Camera Metal Preview";
      window_.releasedWhenClosed = NO;

      view_ = [[NSView alloc] initWithFrame:window_.contentView.bounds];
      view_.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
      view_.wantsLayer = YES;
      layer_ = [CAMetalLayer layer];
      layer_.device = context_->device;
      layer_.pixelFormat = MTLPixelFormatBGRA8Unorm;
      layer_.framebufferOnly = YES;
      layer_.maximumDrawableCount = 3;
      layer_.displaySyncEnabled = YES;
      layer_.presentsWithTransaction = NO;
      layer_.allowsNextDrawableTimeout = YES;
      view_.layer = layer_;
      window_.contentView = view_;

      pipeline_ = make_preview_pipeline(context_->device);
      preview_queue_ = [context_->device newCommandQueue];
      preview_queue_.label = @"swim.preview.command-queue";
      if (preview_queue_ == nil) {
        throw std::runtime_error("cannot create Metal preview command queue");
      }
      auto* sampler_descriptor = [[MTLSamplerDescriptor alloc] init];
      sampler_descriptor.minFilter = MTLSamplerMinMagFilterLinear;
      sampler_descriptor.magFilter = MTLSamplerMinMagFilterLinear;
      sampler_descriptor.sAddressMode = MTLSamplerAddressModeClampToEdge;
      sampler_descriptor.tAddressMode = MTLSamplerAddressModeClampToEdge;
      sampler_ =
          [context_->device newSamplerStateWithDescriptor:sampler_descriptor];
      if (sampler_ == nil) {
        throw std::runtime_error("cannot create Metal preview sampler");
      }

      const std::weak_ptr<Impl> weak = shared_from_this();
      delegate_ = [[SwimPreviewDelegate alloc] init];
      delegate_.closeHandler = ^{
        if (auto owner = weak.lock()) {
          owner->window_closed();
        }
      };
      window_.delegate = delegate_;

      timer_target_ = [[SwimPreviewTimerTarget alloc] init];
      timer_target_.tickHandler = ^{
        if (auto owner = weak.lock()) {
          owner->display_tick();
        }
      };
      timer_ = [NSTimer timerWithTimeInterval:(1.0 / 60.0)
                                       target:timer_target_
                                     selector:@selector(tick:)
                                     userInfo:nil
                                      repeats:YES];
      [[NSRunLoop mainRunLoop] addTimer:timer_ forMode:NSRunLoopCommonModes];
      [window_ makeKeyAndOrderFront:nil];
      [application_ activateIgnoringOtherApps:YES];
      update_drawable_size();
    }
  }

  bool offer(MetalOutputLease output) noexcept {
    if (!output) {
      return false;
    }
    if (metrics_ != nullptr) {
      metrics_->preview_submissions.fetch_add(1,
                                              std::memory_order_relaxed);
    }
    const auto accepted = mailbox_.offer(std::move(output));
    if (!accepted) {
      record_preview_drop();
    }
    return accepted;
  }

  void run_main_loop(std::stop_token token) {
    require_main_thread("Metal preview event loop");
    if (token.stop_requested() || stop_requested_.load(std::memory_order_acquire)) {
      return;
    }
    @autoreleasepool {
      [application_ run];
    }
  }

  void request_stop() noexcept {
    stop_requested_.store(true, std::memory_order_release);
    auto stop = ^{
      [NSApp stop:nil];
      auto* event = [NSEvent
          otherEventWithType:NSEventTypeApplicationDefined
                    location:NSZeroPoint
               modifierFlags:0
                   timestamp:0
                windowNumber:0
                     context:nil
                     subtype:0
                       data1:0
                       data2:0];
      [NSApp postEvent:event atStart:NO];
    };
    if ([NSThread isMainThread]) {
      stop();
    } else {
      dispatch_async(dispatch_get_main_queue(), stop);
    }
  }

  void close_and_drain() {
    require_main_thread("Metal preview shutdown");
    if (closed_.exchange(true, std::memory_order_acq_rel)) {
      flush_metrics();
      return;
    }
    stop_requested_.store(true, std::memory_order_release);
    if (mailbox_.close_and_clear()) {
      record_preview_drop();
    }
    [timer_ invalidate];
    timer_ = nil;
    if (!present_gate_.close_and_wait_until(
            std::chrono::steady_clock::now() + kDrainTimeout)) {
      if (present_gate_.pending() != 0) {
        if (present_accounting_.settle_dropped()) {
          record_preview_drop();
          record_preview_completion(false);
        }
      }
      flush_metrics();
      destroy_ui();
      throw std::runtime_error("timed out waiting for Metal preview presentation");
    }
    flush_metrics();
    destroy_ui();
  }

  ~Impl() {
    if ([NSThread isMainThread] && !closed_.load(std::memory_order_acquire)) {
      try {
        close_and_drain();
      } catch (...) {
      }
    } else {
      flush_metrics();
    }
  }

 private:
  void record_preview_drop() noexcept {
    preview_drops_.fetch_add(1, std::memory_order_relaxed);
    if (metrics_ != nullptr) {
      metrics_->preview_drops.fetch_add(1, std::memory_order_relaxed);
    }
  }

  void record_preview_completion(bool presented) noexcept {
    if (metrics_ == nullptr) {
      return;
    }
    metrics_->preview_completions.fetch_add(1,
                                            std::memory_order_relaxed);
    if (presented) {
      metrics_->preview_presents.fetch_add(1, std::memory_order_relaxed);
    }
  }

  void window_closed() noexcept {
    stop_requested_.store(true, std::memory_order_release);
    try {
      if (close_callback_) {
        close_callback_();
      }
    } catch (...) {
    }
    [NSApp stop:nil];
  }

  void update_drawable_size() noexcept {
    const auto bounds = view_.bounds;
    const auto scale = window_.backingScaleFactor;
    layer_.frame = bounds;
    layer_.contentsScale = scale;
    layer_.drawableSize = CGSizeMake(std::max<CGFloat>(1.0, bounds.size.width * scale),
                                     std::max<CGFloat>(1.0, bounds.size.height * scale));
  }

  void display_tick() noexcept {
    if (closed_.load(std::memory_order_acquire) ||
        stop_requested_.load(std::memory_order_acquire)) {
      return;
    }
    bool expected = false;
    if (!present_in_flight_.compare_exchange_strong(
            expected, true, std::memory_order_acquire,
            std::memory_order_relaxed)) {
      return;
    }
    MetalOutputLease output;
    if (!mailbox_.consume_latest(output)) {
      present_in_flight_.store(false, std::memory_order_release);
      return;
    }
    if (window_.miniaturized || !window_.visible ||
        (window_.occlusionState & NSWindowOcclusionStateVisible) == 0) {
      record_preview_drop();
      present_in_flight_.store(false, std::memory_order_release);
      return;
    }
    update_drawable_size();
    id<CAMetalDrawable> drawable = [layer_ nextDrawable];
    if (drawable == nil || !present_gate_.try_accept()) {
      record_preview_drop();
      present_in_flight_.store(false, std::memory_order_release);
      return;
    }

    id<MTLCommandBuffer> command = [preview_queue_ commandBuffer];
    if (command == nil) {
      record_preview_drop();
      present_gate_.complete();
      present_in_flight_.store(false, std::memory_order_release);
      return;
    }
    native_command_buffers_.fetch_add(1, std::memory_order_relaxed);
    auto* pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = drawable.texture;
    pass.colorAttachments[0].loadAction = MTLLoadActionClear;
    pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    pass.colorAttachments[0].clearColor = MTLClearColorMake(0.0, 0.0, 0.0, 1.0);
    id<MTLRenderCommandEncoder> encoder =
        [command renderCommandEncoderWithDescriptor:pass];
    if (encoder == nil) {
      record_preview_drop();
      present_gate_.complete();
      present_in_flight_.store(false, std::memory_order_release);
      return;
    }

    const auto drawable_size = layer_.drawableSize;
    const auto source_aspect = static_cast<double>(source_width_) /
                               static_cast<double>(source_height_);
    const auto drawable_aspect = drawable_size.width / drawable_size.height;
    PreviewScale scale{1.0F, 1.0F};
    if (drawable_aspect > source_aspect) {
      scale.x = static_cast<float>(source_aspect / drawable_aspect);
    } else {
      scale.y = static_cast<float>(drawable_aspect / source_aspect);
    }
    [encoder setRenderPipelineState:pipeline_];
    [encoder setVertexBytes:&scale length:sizeof(scale) atIndex:0];
    [encoder setFragmentTexture:output.texture() atIndex:0];
    [encoder setFragmentSamplerState:sampler_ atIndex:0];
    [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
    [encoder endEncoding];
    if (!present_accounting_.begin()) {
      record_preview_drop();
      present_gate_.complete();
      present_in_flight_.store(false, std::memory_order_release);
      return;
    }
    [command presentDrawable:drawable];

    auto owner = shared_from_this();
    MetalOutputLease retained_output = std::move(output);
    native_callback_wrappers_.fetch_add(1, std::memory_order_relaxed);
    [command addCompletedHandler:^(id<MTLCommandBuffer> completed) {
      static_cast<void>(retained_output.texture());
      if (completed.status == MTLCommandBufferStatusCompleted) {
        if (owner->present_accounting_.settle_presented()) {
          owner->record_preview_completion(true);
        }
      } else {
        if (owner->present_accounting_.settle_dropped()) {
          owner->record_preview_drop();
          owner->record_preview_completion(false);
        }
      }
      owner->present_gate_.complete();
      owner->present_in_flight_.store(false, std::memory_order_release);
    }];
    [command commit];
  }

  void flush_metrics() noexcept {
    if (metrics_flushed_.exchange(true, std::memory_order_acq_rel)) {
      return;
    }
    auto* metrics = std::exchange(metrics_, nullptr);
    if (metrics == nullptr) {
      return;
    }
    metrics->native_command_buffers.fetch_add(
        native_command_buffers_.load(std::memory_order_relaxed),
        std::memory_order_relaxed);
    metrics->native_callback_wrappers.fetch_add(
        native_callback_wrappers_.load(std::memory_order_relaxed),
        std::memory_order_relaxed);
  }

  void destroy_ui() noexcept {
    window_.delegate = nil;
    delegate_.closeHandler = nil;
    timer_target_.tickHandler = nil;
    [window_ orderOut:nil];
    [window_ close];
    timer_target_ = nil;
    delegate_ = nil;
    view_ = nil;
    layer_ = nil;
    window_ = nil;
    pipeline_ = nil;
    sampler_ = nil;
    preview_queue_ = nil;
  }

  std::shared_ptr<MetalContext> context_;
  const std::uint32_t source_width_;
  const std::uint32_t source_height_;
  swim::core::RuntimeCounters* metrics_;
  CloseCallback close_callback_;
  PreviewMailbox<MetalOutputLease> mailbox_;
  swim::core::RenderCompletionGate present_gate_;
  std::atomic_uint64_t preview_drops_{0};
  std::atomic_uint64_t native_command_buffers_{0};
  std::atomic_uint64_t native_callback_wrappers_{0};
  PreviewPresentationAccounting present_accounting_;
  std::atomic_bool present_in_flight_{false};
  std::atomic_bool stop_requested_{false};
  std::atomic_bool closed_{false};
  std::atomic_bool metrics_flushed_{false};
  NSApplication* application_ = nil;
  NSWindow* window_ = nil;
  NSView* view_ = nil;
  CAMetalLayer* layer_ = nil;
  id<MTLRenderPipelineState> pipeline_ = nil;
  id<MTLSamplerState> sampler_ = nil;
  id<MTLCommandQueue> preview_queue_ = nil;
  SwimPreviewDelegate* delegate_ = nil;
  SwimPreviewTimerTarget* timer_target_ = nil;
  NSTimer* timer_ = nil;
};

MetalPreview::MetalPreview(std::shared_ptr<MetalContext> context,
                           std::uint32_t width, std::uint32_t height,
                           swim::core::RuntimeCounters& metrics,
                           CloseCallback close_callback)
    : impl_(std::make_shared<Impl>(std::move(context), width, height, metrics,
                                   std::move(close_callback))) {
  impl_->initialize();
}

MetalPreview::~MetalPreview() {
  if (impl_ != nullptr && [NSThread isMainThread]) {
    try {
      impl_->close_and_drain();
    } catch (...) {
    }
  }
}

bool MetalPreview::offer(MetalOutputLease output) noexcept {
  return impl_->offer(std::move(output));
}

void MetalPreview::run_main_loop(std::stop_token token) {
  impl_->run_main_loop(token);
}

void MetalPreview::request_stop() noexcept { impl_->request_stop(); }

void MetalPreview::close_and_drain() { impl_->close_and_drain(); }

}  // namespace swim::metal
