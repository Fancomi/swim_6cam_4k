#include <swim/metal/metal_renderer.hpp>

#import <CoreGraphics/CoreGraphics.h>
#import <CoreVideo/CoreVideo.h>
#import <Foundation/Foundation.h>
#import <ImageIO/ImageIO.h>
#import <Metal/Metal.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <filesystem>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

std::shared_ptr<swim::metal::MetalContext> make_context() {
  auto context = std::make_shared<swim::metal::MetalContext>();
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
  if (status != kCVReturnSuccess) {
    throw std::runtime_error("cannot create Metal texture cache");
  }
  return context;
}

void verify_output_pool_contract(
    const std::shared_ptr<swim::metal::MetalContext>& context) {
  swim::metal::MetalOutputPool pool(context, 1, 16, 16);
  auto first = pool.try_acquire();
  if (!first || first->pixel_buffer() == nullptr || first->texture() == nil ||
      pool.high_water() != 1 || pool.try_acquire().has_value()) {
    throw std::runtime_error("Metal output pool acquisition contract failed");
  }
  auto copy = *first;
  first.reset();
  if (pool.try_acquire().has_value()) {
    throw std::runtime_error("copied Metal output lease released too early");
  }
  copy = {};
  if (!pool.try_acquire().has_value()) {
    throw std::runtime_error("Metal output pool did not recycle its slot");
  }
}

void retain_test_frame(void*) noexcept {}
void release_test_frame(void*) noexcept {}

void verify_camera_metadata_contract(
    swim::metal::MetalStitchRenderer& renderer,
    const std::array<swim::metal::MetalFrameView, 6>& valid_frames) {
  swim::metal::MetalRenderResult missing_submission;
  try {
    renderer.wait_for_completion(missing_submission);
    throw std::runtime_error(
        "diagnostic wait accepted a result without a submission");
  } catch (const std::invalid_argument&) {
  }

  auto invalid_frames = valid_frames;
  invalid_frames[0].metadata.camera_index = 1;
  swim::metal::MetalRenderResult result;
  if (renderer.submit(invalid_frames, result)) {
    renderer.wait_for_completion(result);
    throw std::runtime_error(
        "direct Metal views accepted camera metadata in the wrong slot");
  }

  swim::core::RenderSnapshot snapshot;
  constexpr swim::core::NativeLeaseOps kTestLeaseOps{
      retain_test_frame, release_test_frame, swim::metal::kMetalFrameBackendTag};
  for (std::size_t index = 0; index < valid_frames.size(); ++index) {
    auto metadata = valid_frames[index].metadata;
    if (index == 4) {
      metadata.camera_index = 0;
    }
    snapshot.frames[index] = swim::core::FrameLease{
        const_cast<swim::metal::MetalFrameView*>(&valid_frames[index]),
        kTestLeaseOps, metadata};
  }
  if (renderer.submit(snapshot, result)) {
    renderer.wait_for_completion(result);
    throw std::runtime_error(
        "Metal snapshot accepted camera metadata in the wrong slot");
  }
}

id<MTLTexture> make_constant_plane(id<MTLDevice> device,
                                   MTLPixelFormat format,
                                   NSUInteger width,
                                   NSUInteger height,
                                   const void* bytes,
                                   NSUInteger bytes_per_row) {
  auto* descriptor = [MTLTextureDescriptor
      texture2DDescriptorWithPixelFormat:format
                                  width:width
                                 height:height
                              mipmapped:NO];
  descriptor.storageMode = MTLStorageModeShared;
  descriptor.usage = MTLTextureUsageShaderRead;
  id<MTLTexture> texture = [device newTextureWithDescriptor:descriptor];
  if (texture == nil) {
    throw std::runtime_error("cannot create synthetic NV12 texture plane");
  }
  [texture replaceRegion:MTLRegionMake2D(0, 0, width, height)
              mipmapLevel:0
                withBytes:bytes
              bytesPerRow:bytes_per_row];
  return texture;
}

std::array<std::uint8_t, 3> render_full_range_nv12_sample(
    swim::metal::MetalStitchRenderer& renderer,
    const std::shared_ptr<swim::metal::MetalContext>& context,
    std::uint8_t y, std::uint8_t cb, std::uint8_t cr) {
  const std::array<std::uint8_t, 4> luma{y, y, y, y};
  const std::array<std::uint8_t, 2> chroma{cb, cr};
  id<MTLTexture> luma_texture = make_constant_plane(
      context->device, MTLPixelFormatR8Unorm, 2, 2, luma.data(), 2);
  id<MTLTexture> chroma_texture = make_constant_plane(
      context->device, MTLPixelFormatRG8Unorm, 1, 1, chroma.data(), 2);
  std::array<swim::metal::MetalFrameView, 6> frames;
  for (std::size_t index = 0; index < frames.size(); ++index) {
    frames[index].luma = luma_texture;
    frames[index].chroma = chroma_texture;
    frames[index].metadata.camera_index = static_cast<std::uint32_t>(index);
    frames[index].metadata.width = 2;
    frames[index].metadata.height = 2;
    frames[index].metadata.pixel_format =
        swim::core::PixelFormat::nv12_full_range;
    frames[index].metadata.color_matrix = swim::core::ColorMatrix::bt709;
  }
  swim::metal::MetalRenderResult result;
  if (!renderer.submit(frames, result)) {
    throw std::runtime_error("renderer rejected synthetic NV12 frame");
  }
  renderer.wait_for_completion(result);
  if (result.gpu_start_ns == 0 || result.gpu_end_ns < result.gpu_start_ns ||
      renderer.has_fatal_error()) {
    throw std::runtime_error(
        "synthetic NV12 submission did not report exact GPU completion");
  }
  auto* pixel_buffer = result.output.pixel_buffer();
  if (CVPixelBufferLockBaseAddress(pixel_buffer, kCVPixelBufferLock_ReadOnly) !=
      kCVReturnSuccess) {
    throw std::runtime_error("cannot lock synthetic NV12 output");
  }
  const auto* row = static_cast<const std::uint8_t*>(
      CVPixelBufferGetBaseAddress(pixel_buffer)) +
      1050 * CVPixelBufferGetBytesPerRow(pixel_buffer);
  const auto* bgra = row + 2500 * 4;
  const std::array<std::uint8_t, 3> rgb{bgra[2], bgra[1], bgra[0]};
  CVPixelBufferUnlockBaseAddress(pixel_buffer, kCVPixelBufferLock_ReadOnly);
  return rgb;
}

std::uint8_t quantize_unorm(float value) {
  return static_cast<std::uint8_t>(std::lround(
      std::clamp(value, 0.0F, 1.0F) * 255.0F));
}

void verify_full_range_nv12_shader(
    swim::metal::MetalStitchRenderer& renderer,
    const std::shared_ptr<swim::metal::MetalContext>& context) {
  const auto neutral = render_full_range_nv12_sample(
      renderer, context, 128, 128, 128);
  if (neutral != std::array<std::uint8_t, 3>{128, 128, 128}) {
    throw std::runtime_error(
        "full-range NV12 neutral chroma is not exactly neutral");
  }

  constexpr std::uint8_t kY = 250;
  constexpr std::uint8_t kCb = 0;
  constexpr std::uint8_t kCr = 128;
  const auto actual = render_full_range_nv12_sample(
      renderer, context, kY, kCb, kCr);
  const auto luma = static_cast<float>(kY) / 255.0F;
  const auto cb = (static_cast<float>(kCb) - 128.0F) / 254.0F;
  const auto cr = (static_cast<float>(kCr) - 128.0F) / 254.0F;
  const std::array<std::uint8_t, 3> expected{
      quantize_unorm(luma + 1.5748F * cr),
      quantize_unorm(luma - 0.187324F * cb - 0.468124F * cr),
      quantize_unorm(luma + 1.8556F * cb)};
  if (actual != expected) {
    throw std::runtime_error(
        "full-range NV12 non-neutral shader conversion is incorrect");
  }
}

id<MTLTexture> load_rgba_texture(id<MTLDevice> device,
                                 const std::filesystem::path& path) {
  const auto path_string = path.string();
  auto* url = CFURLCreateFromFileSystemRepresentation(
      kCFAllocatorDefault,
      reinterpret_cast<const UInt8*>(path_string.data()),
      static_cast<CFIndex>(path_string.size()),
      false);
  if (url == nullptr) {
    throw std::runtime_error("cannot create texture URL: " + path_string);
  }
  auto* source = CGImageSourceCreateWithURL(url, nullptr);
  CFRelease(url);
  if (source == nullptr) {
    throw std::runtime_error("cannot open texture: " + path_string);
  }
  auto* image = CGImageSourceCreateImageAtIndex(source, 0, nullptr);
  CFRelease(source);
  if (image == nullptr) {
    throw std::runtime_error("cannot decode texture: " + path_string);
  }

  const auto width = CGImageGetWidth(image);
  const auto height = CGImageGetHeight(image);
  const auto bytes_per_row = width * 4;
  std::vector<std::uint8_t> pixels(bytes_per_row * height);
  auto* color_space = CGColorSpaceCreateDeviceRGB();
  auto* bitmap = CGBitmapContextCreate(
      pixels.data(), width, height, 8, bytes_per_row, color_space,
      static_cast<CGBitmapInfo>(kCGImageAlphaPremultipliedLast) |
          static_cast<CGBitmapInfo>(kCGBitmapByteOrder32Big));
  CGColorSpaceRelease(color_space);
  if (bitmap == nullptr) {
    CGImageRelease(image);
    throw std::runtime_error("cannot create texture bitmap: " + path_string);
  }
  CGContextDrawImage(
      bitmap,
      CGRectMake(0, 0, static_cast<CGFloat>(width),
                 static_cast<CGFloat>(height)),
      image);
  CGContextRelease(bitmap);
  CGImageRelease(image);

  auto* descriptor = [MTLTextureDescriptor
      texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                  width:width
                                 height:height
                              mipmapped:NO];
  descriptor.usage = MTLTextureUsageShaderRead;
  descriptor.storageMode = MTLStorageModeShared;
  id<MTLTexture> texture = [device newTextureWithDescriptor:descriptor];
  if (texture == nil) {
    throw std::runtime_error("cannot create texture: " + path_string);
  }
  [texture replaceRegion:MTLRegionMake2D(0, 0, width, height)
              mipmapLevel:0
                withBytes:pixels.data()
              bytesPerRow:bytes_per_row];
  return texture;
}

void write_png(CVPixelBufferRef pixel_buffer,
               const std::filesystem::path& path) {
  if (pixel_buffer == nullptr) {
    throw std::runtime_error("renderer returned no output pixel buffer");
  }
  const auto status = CVPixelBufferLockBaseAddress(
      pixel_buffer, kCVPixelBufferLock_ReadOnly);
  if (status != kCVReturnSuccess) {
    throw std::runtime_error("cannot lock diagnostic output pixel buffer");
  }
  const auto width = CVPixelBufferGetWidth(pixel_buffer);
  const auto height = CVPixelBufferGetHeight(pixel_buffer);
  auto* color_space = CGColorSpaceCreateDeviceRGB();
  auto* bitmap = CGBitmapContextCreate(
      CVPixelBufferGetBaseAddress(pixel_buffer), width, height, 8,
      CVPixelBufferGetBytesPerRow(pixel_buffer), color_space,
      static_cast<CGBitmapInfo>(kCGImageAlphaPremultipliedFirst) |
          static_cast<CGBitmapInfo>(kCGBitmapByteOrder32Little));
  CGColorSpaceRelease(color_space);
  if (bitmap == nullptr) {
    CVPixelBufferUnlockBaseAddress(pixel_buffer,
                                   kCVPixelBufferLock_ReadOnly);
    throw std::runtime_error("cannot create diagnostic output bitmap");
  }
  auto* image = CGBitmapContextCreateImage(bitmap);
  CGContextRelease(bitmap);
  if (image == nullptr) {
    CVPixelBufferUnlockBaseAddress(pixel_buffer,
                                   kCVPixelBufferLock_ReadOnly);
    throw std::runtime_error("cannot create diagnostic output image");
  }

  const auto path_string = path.string();
  auto* url = CFURLCreateFromFileSystemRepresentation(
      kCFAllocatorDefault,
      reinterpret_cast<const UInt8*>(path_string.data()),
      static_cast<CFIndex>(path_string.size()),
      false);
  auto* destination = url == nullptr
                          ? nullptr
                          : CGImageDestinationCreateWithURL(
                                url, CFSTR("public.png"), 1, nullptr);
  if (url != nullptr) {
    CFRelease(url);
  }
  if (destination == nullptr) {
    CGImageRelease(image);
    CVPixelBufferUnlockBaseAddress(pixel_buffer,
                                   kCVPixelBufferLock_ReadOnly);
    throw std::runtime_error("cannot create PNG destination: " + path_string);
  }
  CGImageDestinationAddImage(destination, image, nullptr);
  const auto wrote = CGImageDestinationFinalize(destination);
  CFRelease(destination);
  CGImageRelease(image);
  CVPixelBufferUnlockBaseAddress(pixel_buffer, kCVPixelBufferLock_ReadOnly);
  if (!wrote) {
    throw std::runtime_error("cannot write PNG: " + path_string);
  }
}

}  // namespace

int main(int argc, const char* argv[]) {
  constexpr int kExpectedArgumentCount = 9;
  if (argc != kExpectedArgumentCount) {
    std::cerr << "usage: metal_golden_test ASSET OUTPUT.png TEXTURE_CAM3 "
                 "TEXTURE_CAM2 TEXTURE_CAM1 TEXTURE_CAM4 TEXTURE_CAM5 "
                 "TEXTURE_CAM6\n";
    return 2;
  }
  static_cast<void>(argv);
  @autoreleasepool {
    try {
      auto context = make_context();
      verify_output_pool_contract(context);
      const auto asset = swim::core::load_asset(argv[1]);
      swim::core::AppConfig config;
      config.render_inflight = 2;
      config.output_pool = 2;

      std::array<swim::metal::MetalFrameView, 6> frames;
      for (std::size_t index = 0; index < frames.size(); ++index) {
        frames[index].rgba = load_rgba_texture(context->device, argv[index + 3]);
        frames[index].metadata.camera_index =
            static_cast<std::uint32_t>(index);
        frames[index].metadata.width =
            static_cast<std::uint32_t>(frames[index].rgba.width);
        frames[index].metadata.height =
            static_cast<std::uint32_t>(frames[index].rgba.height);
        frames[index].metadata.pixel_format = swim::core::PixelFormat::bgra8;
      }

      swim::metal::MetalStitchRenderer renderer(context, asset, config);
      verify_camera_metadata_contract(renderer, frames);
      verify_full_range_nv12_shader(renderer, context);
      swim::metal::MetalRenderResult result;
      if (!renderer.submit(frames, result)) {
        throw std::runtime_error("renderer rejected the diagnostic frame");
      }
      renderer.wait_for_completion(result);
      if (result.gpu_start_ns == 0 ||
          result.gpu_end_ns < result.gpu_start_ns ||
          renderer.has_fatal_error()) {
        throw std::runtime_error(
            "diagnostic render did not report exact GPU completion");
      }
      write_png(result.output.pixel_buffer(), argv[2]);
      std::cout << "gpu_start_ns=" << result.gpu_start_ns
                << " gpu_end_ns=" << result.gpu_end_ns << '\n';
      return 0;
    } catch (const std::exception& error) {
      std::cerr << "metal_golden_test: " << error.what() << '\n';
      return 1;
    }
  }
}
