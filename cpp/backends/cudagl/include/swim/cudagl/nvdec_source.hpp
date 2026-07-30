#pragma once

#include <swim/core/config.hpp>
#include <swim/core/latest_frame_mailbox.hpp>
#include <swim/core/metrics.hpp>
#include <swim/core/run_lifecycle.hpp>
#include <swim/cudagl/cudagl_frame.hpp>

#include <cstdint>
#include <memory>
#include <string>

namespace swim::cudagl {

// One lane-local NVDEC decode source built on FFmpeg's h264_cuvid decoder.
// Compressed H.264 is read with libavformat; frames are decoded straight into
// CUDA device memory (AV_PIX_FMT_CUDA NV12) and published as leases carrying
// the CUdeviceptr planes — no host copy. The caller owns the mailbox/counters
// and must keep them alive until wait() returns.
class NvdecSource final {
 public:
  NvdecSource(std::shared_ptr<CudaGlContext> context,
              swim::core::SourceConfig source, std::uint32_t camera_index,
              swim::core::LatestFrameMailbox& mailbox,
              swim::core::RuntimeCounters& counters, swim::core::RunMode mode,
              std::uint32_t surface_capacity,
              swim::core::RunLifecycle* lifecycle,
              // Rewind to the clip start on EOF instead of failing the lane, so
              // a run can outlast the recording. Ignored by live sources.
              bool loop_sources = false);
  ~NvdecSource();

  NvdecSource(const NvdecSource&) = delete;
  NvdecSource& operator=(const NvdecSource&) = delete;

  void start();
  void stop() noexcept;
  void wait();

  bool running() const noexcept;
  bool failed() const noexcept;
  std::string last_error() const;

 private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace swim::cudagl
