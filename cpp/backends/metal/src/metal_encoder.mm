#include <swim/metal/metal_encoder.hpp>

#import <Foundation/Foundation.h>
#import <VideoToolbox/VideoToolbox.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <utility>

namespace swim::metal {
namespace {

constexpr std::array<std::uint8_t, 4> kAnnexBStartCode{0, 0, 0, 1};
constexpr std::uint32_t kEncoderInputCapacity = 2;
constexpr auto kDrainTimeout = std::chrono::seconds{2};

bool valid_nal_length_width(std::uint8_t width) noexcept {
  return width == 1 || width == 2 || width == 4;
}

bool append_nal(std::span<const std::uint8_t> nal,
                AnnexBWriter writer) noexcept {
  return !nal.empty() && writer.append != nullptr &&
         writer.append(writer.context, kAnnexBStartCode) &&
         writer.append(writer.context, nal);
}

std::uint32_t parse_nal_length(const std::uint8_t* bytes,
                               std::uint8_t width) noexcept {
  std::uint32_t length = 0;
  for (std::uint8_t index = 0; index < width; ++index) {
    length = (length << 8U) | bytes[index];
  }
  return length;
}

std::uint64_t steady_now_ns() noexcept {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::steady_clock::now().time_since_epoch())
          .count());
}

template <class AtomicValue, class Value>
void update_high_water(std::atomic<AtomicValue>& high_water,
                       Value value) noexcept {
  const auto desired = static_cast<AtomicValue>(value);
  auto observed = high_water.load(std::memory_order_relaxed);
  while (observed < desired &&
         !high_water.compare_exchange_weak(observed, desired,
                                           std::memory_order_relaxed,
                                           std::memory_order_relaxed)) {
  }
}

void require_status(OSStatus status, const char* operation) {
  if (status != noErr) {
    throw std::runtime_error(std::string(operation) + " failed with status " +
                             std::to_string(status));
  }
}

}  // namespace

bool write_length_prefixed_nals_as_annex_b(
    std::span<const std::uint8_t> access_unit,
    std::uint8_t nal_length_bytes,
    AnnexBWriter writer) noexcept {
  if (!valid_nal_length_width(nal_length_bytes) || writer.append == nullptr) {
    return false;
  }
  std::size_t offset = 0;
  while (offset < access_unit.size()) {
    if (access_unit.size() - offset < nal_length_bytes) {
      return false;
    }
    const auto length = parse_nal_length(access_unit.data() + offset,
                                         nal_length_bytes);
    offset += nal_length_bytes;
    if (length == 0 || length > access_unit.size() - offset) {
      return false;
    }
    if (!append_nal(access_unit.subspan(offset, length), writer)) {
      return false;
    }
    offset += length;
  }
  return true;
}

bool write_length_prefixed_nals_as_annex_b(
    CMBlockBufferRef access_unit,
    std::size_t access_unit_bytes,
    std::uint8_t nal_length_bytes,
    AnnexBWriter writer) noexcept {
  if (access_unit == nullptr ||
      !valid_nal_length_width(nal_length_bytes) || writer.append == nullptr ||
      access_unit_bytes > CMBlockBufferGetDataLength(access_unit)) {
    return false;
  }
  std::size_t offset = 0;
  std::array<std::uint8_t, 4> length_bytes{};
  while (offset < access_unit_bytes) {
    if (access_unit_bytes - offset < nal_length_bytes ||
        CMBlockBufferCopyDataBytes(access_unit, offset, nal_length_bytes,
                                   length_bytes.data()) != kCMBlockBufferNoErr) {
      return false;
    }
    const auto length = parse_nal_length(length_bytes.data(), nal_length_bytes);
    offset += nal_length_bytes;
    if (length == 0 || length > access_unit_bytes - offset ||
        !writer.append(writer.context, kAnnexBStartCode)) {
      return false;
    }
    std::size_t remaining = length;
    while (remaining != 0) {
      std::size_t contiguous = 0;
      std::size_t total = 0;
      char* pointer = nullptr;
      if (CMBlockBufferGetDataPointer(access_unit, offset, &contiguous, &total,
                                      &pointer) != kCMBlockBufferNoErr ||
          pointer == nullptr || contiguous == 0) {
        return false;
      }
      const auto chunk = std::min(remaining, contiguous);
      if (!writer.append(
              writer.context,
              std::span<const std::uint8_t>{
                  reinterpret_cast<const std::uint8_t*>(pointer), chunk})) {
        return false;
      }
      offset += chunk;
      remaining -= chunk;
    }
  }
  return true;
}

bool write_hevc_access_unit_as_annex_b(
    std::span<const std::uint8_t> access_unit,
    std::uint8_t nal_length_bytes,
    std::span<const std::span<const std::uint8_t>> parameter_sets,
    AnnexBWriter writer) noexcept {
  for (const auto parameter_set : parameter_sets) {
    if (!append_nal(parameter_set, writer)) {
      return false;
    }
  }
  return write_length_prefixed_nals_as_annex_b(access_unit, nal_length_bytes,
                                                writer);
}

EncoderInputGate::Lease::Lease(EncoderInputGate* owner,
                               PoolLease lease) noexcept
    : owner_(owner), lease_(std::move(lease)) {}

EncoderInputGate::Lease::Lease(Lease&& other) noexcept
    : owner_(std::exchange(other.owner_, nullptr)),
      lease_(std::move(other.lease_)) {}

EncoderInputGate::Lease& EncoderInputGate::Lease::operator=(
    Lease&& other) noexcept {
  if (this != &other) {
    release();
    owner_ = std::exchange(other.owner_, nullptr);
    lease_ = std::move(other.lease_);
  }
  return *this;
}

EncoderInputGate::Lease::~Lease() { release(); }

EncoderInputRecord* EncoderInputGate::Lease::operator->() const noexcept {
  return lease_->operator->();
}

EncoderInputRecord& EncoderInputGate::Lease::operator*() const noexcept {
  return lease_->operator*();
}

std::size_t EncoderInputGate::Lease::index() const noexcept {
  return lease_->index();
}

void EncoderInputGate::Lease::release() noexcept {
  if (owner_ != nullptr && lease_.has_value()) {
    owner_->release_unarmed(*lease_);
  }
  lease_.reset();
  owner_ = nullptr;
}

EncoderInputGate::EncoderInputGate(std::uint32_t capacity)
    : pool_(capacity), tickets_(std::make_unique<Ticket[]>(capacity)) {
  for (std::uint32_t index = 0; index < capacity; ++index) {
    tickets_[index].owner = this;
  }
}

std::optional<EncoderInputGate::Lease> EncoderInputGate::try_acquire()
    noexcept {
  if (!accepting_.load(std::memory_order_acquire)) {
    misses_.fetch_add(1, std::memory_order_relaxed);
    return std::nullopt;
  }
  auto lease = pool_.try_acquire();
  if (!lease.has_value()) {
    misses_.fetch_add(1, std::memory_order_relaxed);
    return std::nullopt;
  }
  const auto current = in_use_.fetch_add(1, std::memory_order_relaxed) + 1;
  update_high_water(high_water_, current);
  Lease wrapped{this, std::move(*lease)};
  return std::optional<Lease>{std::move(wrapped)};
}

EncoderInputGate::Ticket* EncoderInputGate::arm(Lease&& input) noexcept {
  if (input.owner_ != this || !input.lease_.has_value()) {
    return nullptr;
  }
  auto& ticket = tickets_[input.lease_->index()];
  if (ticket.lease.has_value()) {
    std::terminate();
  }
  ticket.lease.emplace(std::move(*input.lease_));
  input.lease_.reset();
  input.owner_ = nullptr;
  return &ticket;
}

EncoderInputRecord* EncoderInputGate::record(Ticket* ticket) noexcept {
  if (ticket == nullptr || ticket->owner != this || !ticket->lease.has_value()) {
    return nullptr;
  }
  return ticket->lease->operator->();
}

void EncoderInputGate::settle(Ticket* ticket) noexcept {
  if (ticket == nullptr || ticket->owner != this || !ticket->lease.has_value()) {
    return;
  }
  ticket->lease->operator->()->output = {};
  ticket->lease.reset();
  record_release();
}

void EncoderInputGate::close() noexcept {
  accepting_.store(false, std::memory_order_release);
}

bool EncoderInputGate::wait_until_empty(
    std::chrono::milliseconds timeout) noexcept {
  std::unique_lock lock(mutex_);
  return condition_.wait_for(lock, timeout, [this] {
    return in_use_.load(std::memory_order_acquire) == 0;
  });
}

std::uint32_t EncoderInputGate::capacity() const noexcept {
  return static_cast<std::uint32_t>(pool_.capacity());
}

std::uint32_t EncoderInputGate::in_use() const noexcept {
  return in_use_.load(std::memory_order_acquire);
}

std::uint32_t EncoderInputGate::high_water() const noexcept {
  return high_water_.load(std::memory_order_relaxed);
}

std::uint64_t EncoderInputGate::misses() const noexcept {
  return misses_.load(std::memory_order_relaxed);
}

void EncoderInputGate::release_unarmed(PoolLease& lease) noexcept {
  lease->output = {};
  record_release();
}

void EncoderInputGate::record_release() noexcept {
  const auto previous = in_use_.fetch_sub(1, std::memory_order_release);
  if (previous == 1) {
    std::lock_guard lock(mutex_);
    condition_.notify_all();
  }
}

class MetalEncoder::Impl final
    : public std::enable_shared_from_this<MetalEncoder::Impl> {
 public:
  struct CallbackTicket final {
    std::shared_ptr<Impl> owner;
    EncoderInputGate::Ticket* gate_ticket{};
  };

  Impl(std::uint32_t width, std::uint32_t height,
       const swim::core::AppConfig& config,
       swim::core::RuntimeCounters& metrics)
      : gate_(kEncoderInputCapacity),
        callback_tickets_(std::make_unique<CallbackTicket[]>(
            kEncoderInputCapacity)),
        metrics_(metrics),
        null_sink_(config.encode_sink == swim::core::EncodeSink::null_sink) {
    if (width != 5002 || height != 2102) {
      throw std::runtime_error("HEVC encoder requires exact 5002x2102 output");
    }
    metrics_.encode_input_capacity.store(kEncoderInputCapacity,
                                         std::memory_order_relaxed);
    if (!null_sink_) {
      const auto parent = config.encode_path.parent_path();
      if (!parent.empty()) {
        std::filesystem::create_directories(parent);
      }
      file_ = std::fopen(config.encode_path.string().c_str(), "wb");
      if (file_ == nullptr) {
        throw std::runtime_error("cannot open HEVC output: " +
                                 config.encode_path.string());
      }
    }

    const void* keys[] = {
        kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder,
        kVTVideoEncoderSpecification_EnableHardwareAcceleratedVideoEncoder};
    const void* values[] = {kCFBooleanTrue, kCFBooleanTrue};
    CFDictionaryRef specification = CFDictionaryCreate(
        kCFAllocatorDefault, keys, values, 2,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    const auto create_status = VTCompressionSessionCreate(
        kCFAllocatorDefault, static_cast<int32_t>(width),
        static_cast<int32_t>(height), kCMVideoCodecType_HEVC, specification,
        nullptr, nullptr, &Impl::output_callback, this, &session_);
    CFRelease(specification);
    if (create_status != noErr) {
      close_file();
      require_status(create_status, "VTCompressionSessionCreate");
    }
    try {
      configure_session();
    } catch (...) {
      VTCompressionSessionInvalidate(session_);
      CFRelease(session_);
      session_ = nullptr;
      close_file();
      throw;
    }
  }

  ~Impl() {
    if (session_ != nullptr) {
      VTCompressionSessionInvalidate(session_);
      CFRelease(session_);
    }
    close_file();
  }

  bool offer(MetalOutputLease output, CMTime pts) noexcept {
    if (!output || session_ == nullptr) {
      record_drop();
      return false;
    }
    auto input = gate_.try_acquire();
    if (!input.has_value()) {
      record_drop();
      return false;
    }
    metrics_.encode_input_in_use.store(gate_.in_use(),
                                       std::memory_order_relaxed);
    update_high_water(metrics_.encode_input_high_water, gate_.high_water());
    input->operator->()->output = std::move(output);
    input->operator->()->pts = pts;
    input->operator->()->submission_sequence =
        next_sequence_.fetch_add(1, std::memory_order_relaxed);
    const auto slot = input->index();
    auto* gate_ticket = gate_.arm(std::move(*input));
    auto& callback_ticket = callback_tickets_[slot];
    callback_ticket.owner = shared_from_this();
    callback_ticket.gate_ticket = gate_ticket;

    auto* record = gate_.record(gate_ticket);
    const auto status = VTCompressionSessionEncodeFrame(
        session_, record->output.pixel_buffer(), record->pts,
        kCMTimeInvalid, nullptr, &callback_ticket, nullptr);
    if (status == noErr) {
      const auto now = steady_now_ns();
      std::uint64_t zero = 0;
      first_submit_ns_.compare_exchange_strong(zero, now,
                                               std::memory_order_relaxed);
      zero = 0;
      metrics_.encode_first_submit_ns.compare_exchange_strong(
          zero, now, std::memory_order_relaxed);
      submissions_.fetch_add(1, std::memory_order_relaxed);
      metrics_.encode_submissions.fetch_add(1, std::memory_order_relaxed);
      return true;
    }
    rejected_frames_.fetch_add(1, std::memory_order_relaxed);
    metrics_.encode_rejected_frames.fetch_add(1, std::memory_order_relaxed);
    settle(callback_ticket);
    return false;
  }

  void close_and_drain() {
    bool expected = false;
    if (!closed_.compare_exchange_strong(expected, true,
                                         std::memory_order_acq_rel)) {
      return;
    }
    gate_.close();
    if (session_ != nullptr) {
      const auto status = VTCompressionSessionCompleteFrames(
          session_, kCMTimeInvalid);
      if (status != noErr) {
        mark_fatal("VTCompressionSessionCompleteFrames failed with status " +
                   std::to_string(status));
      }
    }
    if (!gate_.wait_until_empty(
            std::chrono::duration_cast<std::chrono::milliseconds>(
                kDrainTimeout))) {
      drain_timed_out_.store(true, std::memory_order_relaxed);
      metrics_.encode_drain_timeouts.fetch_add(1,
                                               std::memory_order_relaxed);
    }
    publish_gate_metrics();
    if (session_ != nullptr) {
      VTCompressionSessionInvalidate(session_);
      CFRelease(session_);
      session_ = nullptr;
    }
    if (!drain_timed_out_.load(std::memory_order_relaxed)) {
      close_file();
    }
  }

  MetalEncoderStats stats() const noexcept {
    return MetalEncoderStats{
        submissions_.load(std::memory_order_relaxed),
        completions_.load(std::memory_order_relaxed),
        bytes_.load(std::memory_order_relaxed),
        drops_.load(std::memory_order_relaxed),
        rejected_frames_.load(std::memory_order_relaxed),
        callback_errors_.load(std::memory_order_relaxed),
        gate_.capacity(),
        gate_.high_water(),
        gate_.in_use(),
        using_hardware_.load(std::memory_order_relaxed),
        drain_timed_out_.load(std::memory_order_relaxed)};
  }

  bool has_fatal_error() const noexcept {
    return fatal_.load(std::memory_order_acquire);
  }

  std::string fatal_error_message() const {
    std::lock_guard lock(error_mutex_);
    return fatal_message_;
  }

 private:
  static void output_callback(void*, void* source_frame_refcon,
                              OSStatus status,
                              VTEncodeInfoFlags,
                              CMSampleBufferRef sample) noexcept {
    auto* ticket = static_cast<CallbackTicket*>(source_frame_refcon);
    if (ticket == nullptr || ticket->owner == nullptr) {
      return;
    }
    auto owner = ticket->owner;
    owner->handle_output(*ticket, status, sample);
  }

  void handle_output(CallbackTicket& ticket, OSStatus status,
                     CMSampleBufferRef sample) noexcept {
    bool succeeded = false;
    if (status != noErr || sample == nullptr ||
        !CMSampleBufferDataIsReady(sample)) {
      record_callback_error("VideoToolbox returned an invalid HEVC sample");
      settle(ticket);
      return;
    }
    auto format = CMSampleBufferGetFormatDescription(sample);
    auto block = CMSampleBufferGetDataBuffer(sample);
    if (format == nullptr || block == nullptr) {
      record_callback_error("HEVC sample is missing format or payload");
      settle(ticket);
      return;
    }

    const auto attachments = CMSampleBufferGetSampleAttachmentsArray(sample,
                                                                      false);
    bool keyframe = true;
    if (attachments != nullptr && CFArrayGetCount(attachments) != 0) {
      auto dictionary = static_cast<CFDictionaryRef>(
          CFArrayGetValueAtIndex(attachments, 0));
      keyframe = !CFDictionaryContainsKey(
          dictionary, kCMSampleAttachmentKey_NotSync);
    }

    std::array<std::span<const std::uint8_t>, 3> parameter_sets{};
    std::size_t parameter_set_count = 0;
    int nal_length_bytes = 0;
    if (keyframe) {
      std::size_t available_sets = 0;
      for (std::size_t index = 0; index < parameter_sets.size(); ++index) {
        const std::uint8_t* pointer = nullptr;
        std::size_t length = 0;
        const auto ps_status =
            CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(
                format, index, &pointer, &length, &available_sets,
                &nal_length_bytes);
        if (ps_status != noErr || pointer == nullptr || length == 0 ||
            available_sets < parameter_sets.size()) {
          record_callback_error("invalid HEVC VPS/SPS/PPS parameter sets");
          settle(ticket);
          return;
        }
        parameter_sets[index] = {pointer, length};
        ++parameter_set_count;
      }
    } else {
      const std::uint8_t* pointer = nullptr;
      std::size_t length = 0;
      std::size_t available_sets = 0;
      const auto ps_status = CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(
          format, 0, &pointer, &length, &available_sets, &nal_length_bytes);
      if (ps_status != noErr) {
        record_callback_error("cannot read HEVC NAL length width");
        settle(ticket);
        return;
      }
    }
    if (!valid_nal_length_width(static_cast<std::uint8_t>(nal_length_bytes))) {
      record_callback_error("invalid HEVC NAL length width");
      settle(ticket);
      return;
    }

    {
      std::lock_guard lock(writer_mutex_);
      const AnnexBWriter writer{this, &Impl::append_output};
      succeeded = true;
      for (std::size_t index = 0; index < parameter_set_count; ++index) {
        if (!append_nal(parameter_sets[index], writer)) {
          succeeded = false;
          break;
        }
      }
      if (succeeded) {
        succeeded = write_length_prefixed_nals_as_annex_b(
            block, CMBlockBufferGetDataLength(block),
            static_cast<std::uint8_t>(nal_length_bytes), writer);
      }
    }
    if (!succeeded) {
      record_callback_error("cannot write HEVC Annex-B access unit");
    } else {
      completions_.fetch_add(1, std::memory_order_relaxed);
      last_completion_ns_.store(steady_now_ns(), std::memory_order_relaxed);
      metrics_.encode_completions.fetch_add(1, std::memory_order_relaxed);
      metrics_.encode_last_completion_ns.store(steady_now_ns(),
                                                std::memory_order_relaxed);
    }
    settle(ticket);
  }

  static bool append_output(void* context,
                            std::span<const std::uint8_t> bytes) noexcept {
    auto& self = *static_cast<Impl*>(context);
    if (bytes.empty()) {
      return true;
    }
    if (!self.null_sink_ &&
        std::fwrite(bytes.data(), 1, bytes.size(), self.file_) != bytes.size()) {
      self.mark_fatal("cannot write HEVC output");
      return false;
    }
    self.bytes_.fetch_add(bytes.size(), std::memory_order_relaxed);
    self.metrics_.encode_bytes.fetch_add(bytes.size(),
                                         std::memory_order_relaxed);
    return true;
  }

  void configure_session() {
    require_status(VTSessionSetProperty(
                       session_, kVTCompressionPropertyKey_RealTime,
                       kCFBooleanTrue),
                   "set HEVC realtime mode");
    require_status(VTSessionSetProperty(
                       session_, kVTCompressionPropertyKey_AllowFrameReordering,
                       kCFBooleanFalse),
                   "disable HEVC frame reordering");
    require_status(VTSessionSetProperty(
                       session_, kVTCompressionPropertyKey_ProfileLevel,
                       kVTProfileLevel_HEVC_Main_AutoLevel),
                   "set HEVC profile");
    require_status(VTSessionSetProperty(
                       session_, kVTCompressionPropertyKey_ExpectedFrameRate,
                       (__bridge CFTypeRef)@(30000.0 / 1001.0)),
                   "set HEVC expected frame rate");
    require_status(VTSessionSetProperty(
                       session_, kVTCompressionPropertyKey_AverageBitRate,
                       (__bridge CFTypeRef)@(60'000'000)),
                   "set HEVC average bitrate");
    require_status(VTSessionSetProperty(
                       session_, kVTCompressionPropertyKey_MaxKeyFrameInterval,
                       (__bridge CFTypeRef)@(60)),
                   "set HEVC keyframe interval");
    require_status(VTCompressionSessionPrepareToEncodeFrames(session_),
                   "prepare HEVC encoder");
    CFTypeRef hardware = nullptr;
    require_status(VTSessionCopyProperty(
                       session_,
                       kVTCompressionPropertyKey_UsingHardwareAcceleratedVideoEncoder,
                       kCFAllocatorDefault, &hardware),
                   "query HEVC hardware encoder");
    const bool enabled = hardware == kCFBooleanTrue;
    if (hardware != nullptr) {
      CFRelease(hardware);
    }
    if (!enabled) {
      throw std::runtime_error("hardware HEVC encoder is required");
    }
    using_hardware_.store(true, std::memory_order_relaxed);
    metrics_.encode_using_hardware.store(1, std::memory_order_relaxed);
  }

  void settle(CallbackTicket& ticket) noexcept {
    gate_.settle(ticket.gate_ticket);
    metrics_.encode_input_in_use.store(gate_.in_use(),
                                       std::memory_order_relaxed);
    ticket.gate_ticket = nullptr;
    ticket.owner.reset();
  }

  void record_drop() noexcept {
    drops_.fetch_add(1, std::memory_order_relaxed);
    metrics_.encode_drops.fetch_add(1, std::memory_order_relaxed);
    metrics_.encode_input_pool_misses.store(gate_.misses(),
                                            std::memory_order_relaxed);
  }

  void record_callback_error(std::string message) noexcept {
    callback_errors_.fetch_add(1, std::memory_order_relaxed);
    metrics_.encode_callback_errors.fetch_add(1, std::memory_order_relaxed);
    mark_fatal(std::move(message));
  }

  void mark_fatal(std::string message) noexcept {
    bool expected = false;
    if (fatal_.compare_exchange_strong(expected, true,
                                       std::memory_order_acq_rel)) {
      std::lock_guard lock(error_mutex_);
      fatal_message_ = std::move(message);
    }
  }

  void publish_gate_metrics() noexcept {
    metrics_.encode_input_capacity.store(gate_.capacity(),
                                         std::memory_order_relaxed);
    metrics_.encode_input_in_use.store(gate_.in_use(),
                                       std::memory_order_relaxed);
    metrics_.encode_input_high_water.store(gate_.high_water(),
                                           std::memory_order_relaxed);
    metrics_.encode_input_pool_misses.store(gate_.misses(),
                                            std::memory_order_relaxed);
  }

  void close_file() noexcept {
    if (file_ != nullptr) {
      std::fclose(file_);
      file_ = nullptr;
    }
  }

  VTCompressionSessionRef session_{};
  EncoderInputGate gate_;
  std::unique_ptr<CallbackTicket[]> callback_tickets_;
  swim::core::RuntimeCounters& metrics_;
  const bool null_sink_{};
  std::FILE* file_{};
  std::mutex writer_mutex_;
  std::atomic_uint64_t next_sequence_{};
  std::atomic_uint64_t first_submit_ns_{};
  std::atomic_uint64_t last_completion_ns_{};
  std::atomic_uint64_t submissions_{};
  std::atomic_uint64_t completions_{};
  std::atomic_uint64_t bytes_{};
  std::atomic_uint64_t drops_{};
  std::atomic_uint64_t rejected_frames_{};
  std::atomic_uint64_t callback_errors_{};
  std::atomic_bool using_hardware_{};
  std::atomic_bool drain_timed_out_{};
  std::atomic_bool closed_{};
  std::atomic_bool fatal_{};
  mutable std::mutex error_mutex_;
  std::string fatal_message_;
};

MetalEncoder::MetalEncoder(std::uint32_t width, std::uint32_t height,
                           const swim::core::AppConfig& config,
                           swim::core::RuntimeCounters& metrics)
    : impl_(std::make_shared<Impl>(width, height, config, metrics)) {}

MetalEncoder::~MetalEncoder() {
  if (impl_ != nullptr) {
    try {
      impl_->close_and_drain();
    } catch (...) {
    }
  }
}

bool MetalEncoder::offer(MetalOutputLease output, CMTime pts) noexcept {
  return impl_->offer(std::move(output), pts);
}

void MetalEncoder::close_and_drain() { impl_->close_and_drain(); }

MetalEncoderStats MetalEncoder::stats() const noexcept { return impl_->stats(); }

bool MetalEncoder::has_fatal_error() const noexcept {
  return impl_->has_fatal_error();
}

std::string MetalEncoder::fatal_error_message() const {
  return impl_->fatal_error_message();
}

}  // namespace swim::metal
