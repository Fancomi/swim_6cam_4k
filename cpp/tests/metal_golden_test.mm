#include <swim/metal/metal_renderer.hpp>

#import <CoreGraphics/CoreGraphics.h>
#import <CoreVideo/CoreVideo.h>
#import <Foundation/Foundation.h>
#import <ImageIO/ImageIO.h>
#import <Metal/Metal.h>

#include <array>
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
      swim::metal::MetalRenderResult result;
      if (!renderer.submit(frames, result)) {
        throw std::runtime_error("renderer rejected the diagnostic frame");
      }
      renderer.wait_for_completion(result);
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
