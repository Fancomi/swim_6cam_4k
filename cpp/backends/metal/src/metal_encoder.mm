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
#include <string_view>
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
  if (access_unit.empty() || !valid_nal_length_width(nal_length_bytes) ||
      writer.append == nullptr) {
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
  if (access_unit == nullptr || access_unit_bytes == 0 ||
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
  auto* const owner = owner_;
  if (owner_ != nullptr && lease_.has_value()) {
    lease_->operator->()->output = {};
  }
  lease_.reset();
  owner_ = nullptr;
  if (owner != nullptr) {
    owner->record_release();
  }
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

bool EncoderInputGate::wait_until_empty(
    std::chrono::steady_clock::time_point deadline) noexcept {
  std::unique_lock lock(mutex_);
  return condition_.wait_until(lock, deadline, [this] {
    return in_use_.load(std::memory_order_acquire) == 0;
  });
}

void EncoderInputGate::wait_until_empty() noexcept {
  std::unique_lock lock(mutex_);
  condition_.wait(lock, [this] {
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

void EncoderInputGate::record_release() noexcept {
  const auto previous = in_use_.fetch_sub(1, std::memory_order_release);
  if (previous == 1) {
    std::lock_guard lock(mutex_);
    condition_.notify_all();
  }
}

class MetalEncoder::Impl final {
 private:
  class State;

  enum class TicketPhase : std::uint8_t {
    idle,
    armed,
    callback_claimed,
    settled,
    retired,
  };

  struct CallbackTicket final {
    std::shared_ptr<State> state;
    EncoderInputGate::Ticket* gate_ticket{};
    std::atomic_bool drop_recorded{};
    std::atomic_bool frame_dropped{};
    std::atomic<TicketPhase> phase{TicketPhase::idle};
  };

  class State final {
   public:
    State(MetalEncoderWriterOps writer, void* now_context,
          std::uint64_t (*now_ns)(void*) noexcept,
          std::shared_ptr<void> lifetime_anchor)
        : gate(kEncoderInputCapacity),
          writer_(writer),
          now_context_(now_context),
          now_ns_(now_ns),
          lifetime_anchor_(std::move(lifetime_anchor)) {
      fatal_message_.reserve(256);
    }

    ~State() { static_cast<void>(close_writer()); }

    std::uint64_t now() const noexcept {
      return now_ns_ == nullptr ? steady_now_ns() : now_ns_(now_context_);
    }

    bool claim_first_submit(std::uint64_t timestamp) noexcept {
      std::uint64_t zero = 0;
      return first_submit_ns.compare_exchange_strong(
          zero, timestamp, std::memory_order_relaxed);
    }

    void rollback_first_submit(std::uint64_t timestamp,
                               bool claimed) noexcept {
      if (!claimed || submissions.load(std::memory_order_relaxed) != 0 ||
          completions.load(std::memory_order_relaxed) != 0) {
        return;
      }
      first_submit_ns.compare_exchange_strong(
          timestamp, 0, std::memory_order_relaxed);
    }

    void record_drop() noexcept {
      drops.fetch_add(1, std::memory_order_relaxed);
    }

    void record_drop_once(CallbackTicket& ticket) noexcept {
      if (!ticket.drop_recorded.exchange(true, std::memory_order_relaxed)) {
        record_drop();
      }
    }

    void record_frame_drop(CallbackTicket& ticket) noexcept {
      ticket.frame_dropped.store(true, std::memory_order_relaxed);
      record_drop_once(ticket);
    }

    void record_callback_error(CallbackTicket* ticket,
                               std::string_view message) noexcept {
      callback_errors.fetch_add(1, std::memory_order_relaxed);
      if (ticket == nullptr) {
        record_drop();
      } else {
        record_drop_once(*ticket);
      }
      mark_fatal(message);
    }

    void mark_fatal(std::string_view message) noexcept {
      if (fatal.load(std::memory_order_acquire)) {
        return;
      }
      std::lock_guard lock(error_mutex_);
      if (!fatal.load(std::memory_order_relaxed)) {
        fatal_message_.assign(message.data(), message.size());
        fatal.store(true, std::memory_order_release);
      }
    }

    std::string fatal_message() const {
      std::lock_guard lock(error_mutex_);
      return fatal_message_;
    }

    void settle(CallbackTicket& ticket) noexcept {
      auto* const gate_ticket = ticket.gate_ticket;
      ticket.gate_ticket = nullptr;
      ticket.state.reset();
      ticket.phase.store(TicketPhase::settled, std::memory_order_release);
      gate.settle(gate_ticket);
    }

    void handle_injected(CallbackTicket& ticket,
                         const MetalEncoderInjectedOutput& output) noexcept {
      if (output.kind == MetalEncoderInjectedOutputKind::frame_dropped) {
        record_frame_drop(ticket);
        settle(ticket);
        return;
      }
      if (output.kind == MetalEncoderInjectedOutputKind::callback_error) {
        record_callback_error(&ticket, "injected HEVC callback error");
        settle(ticket);
        return;
      }
      bool succeeded = false;
      {
        std::lock_guard lock(writer_mutex_);
        const AnnexBWriter writer{this, &State::append_bridge};
        succeeded = write_hevc_access_unit_as_annex_b(
            output.access_unit, output.nal_length_bytes,
            output.parameter_sets, writer);
      }
      finish_access_unit(ticket, succeeded);
    }

    void handle_native(CallbackTicket& ticket, OSStatus status,
                       VTEncodeInfoFlags info_flags,
                       CMSampleBufferRef sample) noexcept {
      if ((info_flags & kVTEncodeInfo_FrameDropped) != 0) {
        record_frame_drop(ticket);
        settle(ticket);
        return;
      }
      if (status != noErr || sample == nullptr ||
          !CMSampleBufferDataIsReady(sample)) {
        record_callback_error(
            &ticket, "VideoToolbox returned an invalid HEVC sample");
        settle(ticket);
        return;
      }
      auto format = CMSampleBufferGetFormatDescription(sample);
      auto block = CMSampleBufferGetDataBuffer(sample);
      if (format == nullptr || block == nullptr ||
          CMBlockBufferGetDataLength(block) == 0) {
        record_callback_error(&ticket,
                              "HEVC sample is missing format or payload");
        settle(ticket);
        return;
      }

      const auto attachments =
          CMSampleBufferGetSampleAttachmentsArray(sample, false);
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
            record_callback_error(
                &ticket, "invalid HEVC VPS/SPS/PPS parameter sets");
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
        const auto ps_status =
            CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(
                format, 0, &pointer, &length, &available_sets,
                &nal_length_bytes);
        if (ps_status != noErr) {
          record_callback_error(&ticket,
                                "cannot read HEVC NAL length width");
          settle(ticket);
          return;
        }
      }
      if (!valid_nal_length_width(
              static_cast<std::uint8_t>(nal_length_bytes))) {
        record_callback_error(&ticket, "invalid HEVC NAL length width");
        settle(ticket);
        return;
      }

      bool succeeded = true;
      {
        std::lock_guard lock(writer_mutex_);
        const AnnexBWriter writer{this, &State::append_bridge};
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
      finish_access_unit(ticket, succeeded);
    }

    bool close_writer() noexcept {
      bool expected = false;
      if (!writer_closed_.compare_exchange_strong(
              expected, true, std::memory_order_acq_rel)) {
        return true;
      }
      std::lock_guard lock(writer_mutex_);
      const bool succeeded = writer_.close == nullptr ||
                             writer_.close(writer_.context);
      if (!succeeded) {
        record_callback_error(nullptr, "cannot close HEVC output");
      }
      return succeeded;
    }

    EncoderInputGate gate;
    std::atomic_uint64_t next_sequence{};
    std::atomic_uint64_t first_submit_ns{};
    std::atomic_uint64_t last_completion_ns{};
    std::atomic_uint64_t submissions{};
    std::atomic_uint64_t completions{};
    std::atomic_uint64_t bytes{};
    std::atomic_uint64_t drops{};
    std::atomic_uint64_t rejected_frames{};
    std::atomic_uint64_t callback_errors{};
    std::atomic_bool using_hardware{};
    std::atomic_bool drain_timed_out{};
    std::atomic_bool fatal{};

   private:
    static bool append_bridge(
        void* context, std::span<const std::uint8_t> bytes) noexcept {
      auto& self = *static_cast<State*>(context);
      if (bytes.empty()) {
        return true;
      }
      if (self.writer_.append == nullptr ||
          !self.writer_.append(self.writer_.context, bytes)) {
        return false;
      }
      self.bytes.fetch_add(bytes.size(), std::memory_order_relaxed);
      return true;
    }

    void finish_access_unit(CallbackTicket& ticket, bool succeeded) noexcept {
      if (!succeeded) {
        record_callback_error(&ticket,
                              "cannot write HEVC Annex-B access unit");
      } else {
        completions.fetch_add(1, std::memory_order_relaxed);
        last_completion_ns.store(now(), std::memory_order_relaxed);
      }
      settle(ticket);
    }

    MetalEncoderWriterOps writer_;
    void* now_context_{};
    std::uint64_t (*now_ns_)(void*) noexcept{};
    std::shared_ptr<void> lifetime_anchor_;
    std::atomic_bool writer_closed_{};
    std::mutex writer_mutex_;
    mutable std::mutex error_mutex_;
    std::string fatal_message_;
  };

  class SessionHandle final {
   public:
    explicit SessionHandle(MetalEncoderNativeSession native)
        : native_(native) {}
    ~SessionHandle() {
      invalidate();
      if (native_.release != nullptr) {
        native_.release(native_.context);
      }
    }
    OSStatus encode(CVPixelBufferRef pixel_buffer, CMTime pts,
                    void* source_frame, VTEncodeInfoFlags* info_flags,
                    MetalEncoderInjectedCallback callback,
                    void* callback_context) noexcept {
      return native_.encode(native_.context, pixel_buffer, pts, source_frame,
                            info_flags, callback, callback_context);
    }
    OSStatus complete_frames() noexcept {
      return native_.complete_frames(native_.context);
    }
    void invalidate() noexcept {
      if (!invalidated_.exchange(true, std::memory_order_acq_rel) &&
          native_.invalidate != nullptr) {
        native_.invalidate(native_.context);
      }
    }
    bool using_hardware() const noexcept { return native_.using_hardware; }

   private:
    MetalEncoderNativeSession native_;
    std::atomic_bool invalidated_{};
  };

 public:
  Impl(std::uint32_t width, std::uint32_t height,
       const swim::core::AppConfig& config,
       swim::core::RuntimeCounters& metrics)
      : metrics_(&metrics), drain_timeout_(kDrainTimeout) {
    validate_dimensions(width, height);
    auto writer = make_production_writer(config);
    state_ = std::make_shared<State>(writer, nullptr, nullptr, nullptr);
    tickets_ = std::make_unique<CallbackTicket[]>(kEncoderInputCapacity);
    create_production_session(width, height);
  }

  Impl(std::uint32_t width, std::uint32_t height,
       const swim::core::AppConfig&,
       swim::core::RuntimeCounters& metrics,
       MetalEncoderDependencies dependencies)
      : metrics_(&metrics), drain_timeout_(dependencies.drain_timeout) {
    validate_dimensions(width, height);
    validate_dependencies(dependencies);
    state_ = std::make_shared<State>(
        dependencies.writer, dependencies.now_context, dependencies.now_ns,
        std::move(dependencies.lifetime_anchor));
    state_->using_hardware.store(dependencies.native.using_hardware,
                                 std::memory_order_relaxed);
    tickets_ = std::make_unique<CallbackTicket[]>(kEncoderInputCapacity);
    session_ = std::make_shared<SessionHandle>(dependencies.native);
  }

  ~Impl() { close_and_drain(); }

  bool offer(MetalOutputLease output, CMTime pts) noexcept {
    auto state = state_;
    if (!output || session_ == nullptr ||
        state->fatal.load(std::memory_order_acquire) ||
        closed_.load(std::memory_order_acquire)) {
      state->record_drop();
      return false;
    }
    auto input = state->gate.try_acquire();
    if (!input.has_value()) {
      state->record_drop();
      return false;
    }
    input->operator->()->output = std::move(output);
    input->operator->()->pts = pts;
    input->operator->()->submission_sequence =
        state->next_sequence.fetch_add(1, std::memory_order_relaxed);
    const auto slot = input->index();
    auto* gate_ticket = state->gate.arm(std::move(*input));
    auto& callback_ticket = tickets_[slot];
    callback_ticket.drop_recorded.store(false, std::memory_order_relaxed);
    callback_ticket.frame_dropped.store(false, std::memory_order_relaxed);
    callback_ticket.state = state;
    callback_ticket.gate_ticket = gate_ticket;
    callback_ticket.phase.store(TicketPhase::armed,
                                std::memory_order_release);

    auto* record = state->gate.record(gate_ticket);
    const auto submitted_at = state->now();
    const bool claimed_first = state->claim_first_submit(submitted_at);
    VTEncodeInfoFlags info_flags = 0;
    const auto status = session_->encode(
        record->output.pixel_buffer(), record->pts, &callback_ticket,
        &info_flags, &Impl::injected_callback, nullptr);
    if (status != noErr) {
      state->rejected_frames.fetch_add(1, std::memory_order_relaxed);
      if (claim_ticket(callback_ticket)) {
        state->record_drop_once(callback_ticket);
        state->settle(callback_ticket);
      }
      state->rollback_first_submit(submitted_at, claimed_first);
      return false;
    }
    state->submissions.fetch_add(1, std::memory_order_relaxed);
    if ((info_flags & kVTEncodeInfo_FrameDropped) != 0) {
      if (claim_ticket(callback_ticket)) {
        state->record_frame_drop(callback_ticket);
        state->settle(callback_ticket);
      }
      return false;
    }
    return !callback_ticket.frame_dropped.load(std::memory_order_relaxed);
  }

  void close_and_drain() noexcept {
    bool expected = false;
    if (!closed_.compare_exchange_strong(expected, true,
                                         std::memory_order_acq_rel)) {
      return;
    }
    auto state = state_;
    auto session = session_;
    state->gate.close();
    const auto deadline = std::chrono::steady_clock::now() + drain_timeout_;
    const bool callbacks_settled = state->gate.wait_until_empty(deadline);
    if (callbacks_settled) {
      const auto status = session->complete_frames();
      if (status != noErr) {
        state->mark_fatal("VTCompressionSessionCompleteFrames failed");
      }
      static_cast<void>(state->close_writer());
      session->invalidate();
      tickets_.reset();
    } else {
      state->drain_timed_out.store(true, std::memory_order_relaxed);
      session->invalidate();
      const bool callback_in_flight = retire_unclaimed_tickets();
      if (!callback_in_flight) {
        static_cast<void>(state->close_writer());
      }
      // VideoToolbox owns only raw sourceFrameRefCon addresses. After timeout
      // the two fixed records become tiny stable tombstones: retired records
      // no-op, while an already-entered callback owns State until settlement.
      static_cast<void>(tickets_.release());
    }
    flush_metrics();
    session_.reset();
  }

  MetalEncoderStats stats() const noexcept {
    const auto& state = *state_;
    return MetalEncoderStats{
        state.submissions.load(std::memory_order_relaxed),
        state.completions.load(std::memory_order_relaxed),
        state.bytes.load(std::memory_order_relaxed),
        state.drops.load(std::memory_order_relaxed),
        state.rejected_frames.load(std::memory_order_relaxed),
        state.callback_errors.load(std::memory_order_relaxed),
        state.gate.capacity(),
        state.gate.high_water(),
        state.gate.in_use(),
        state.using_hardware.load(std::memory_order_relaxed),
        state.drain_timed_out.load(std::memory_order_relaxed)};
  }

  bool has_fatal_error() const noexcept {
    return state_->fatal.load(std::memory_order_acquire);
  }

  std::string fatal_error_message() const {
    return state_->fatal_message();
  }

 private:
  static void validate_dimensions(std::uint32_t width, std::uint32_t height) {
    if (width != 5002 || height != 2102) {
      throw std::runtime_error("HEVC encoder requires exact 5002x2102 output");
    }
  }

  static void validate_dependencies(
      const MetalEncoderDependencies& dependencies) {
    if (dependencies.native.encode == nullptr ||
        dependencies.native.complete_frames == nullptr ||
        dependencies.native.invalidate == nullptr ||
        dependencies.native.release == nullptr ||
        dependencies.writer.append == nullptr ||
        dependencies.writer.close == nullptr) {
      throw std::invalid_argument("incomplete Metal encoder dependencies");
    }
  }

  static bool file_append(
      void* context, std::span<const std::uint8_t> bytes) noexcept {
    auto* file = static_cast<std::FILE*>(context);
    return std::fwrite(bytes.data(), 1, bytes.size(), file) == bytes.size();
  }

  static bool file_close(void* context) noexcept {
    return std::fclose(static_cast<std::FILE*>(context)) == 0;
  }

  static bool null_append(void*, std::span<const std::uint8_t>) noexcept {
    return true;
  }

  static bool null_close(void*) noexcept { return true; }

  static MetalEncoderWriterOps make_production_writer(
      const swim::core::AppConfig& config) {
    if (config.encode_sink == swim::core::EncodeSink::null_sink) {
      return {nullptr, &Impl::null_append, &Impl::null_close};
    }
    const auto parent = config.encode_path.parent_path();
    if (!parent.empty()) {
      std::filesystem::create_directories(parent);
    }
    auto* file = std::fopen(config.encode_path.string().c_str(), "wb");
    if (file == nullptr) {
      throw std::runtime_error("cannot open HEVC output: " +
                               config.encode_path.string());
    }
    return {file, &Impl::file_append, &Impl::file_close};
  }

  static OSStatus vt_encode(
      void* context, CVPixelBufferRef pixel_buffer, CMTime pts,
      void* source_frame, VTEncodeInfoFlags* info_flags,
      MetalEncoderInjectedCallback, void*) noexcept {
    return VTCompressionSessionEncodeFrame(
        static_cast<VTCompressionSessionRef>(context), pixel_buffer, pts,
        kCMTimeInvalid, nullptr, source_frame, info_flags);
  }

  static OSStatus vt_complete_frames(void* context) noexcept {
    return VTCompressionSessionCompleteFrames(
        static_cast<VTCompressionSessionRef>(context), kCMTimeInvalid);
  }

  static void vt_invalidate(void* context) noexcept {
    VTCompressionSessionInvalidate(
        static_cast<VTCompressionSessionRef>(context));
  }

  static void vt_release(void* context) noexcept {
    CFRelease(static_cast<VTCompressionSessionRef>(context));
  }

  static bool claim_ticket(CallbackTicket& ticket) noexcept {
    auto expected = TicketPhase::armed;
    return ticket.phase.compare_exchange_strong(
        expected, TicketPhase::callback_claimed,
        std::memory_order_acq_rel, std::memory_order_acquire);
  }

  bool retire_unclaimed_tickets() noexcept {
    bool callback_in_flight = false;
    for (std::uint32_t index = 0; index < kEncoderInputCapacity; ++index) {
      auto& ticket = tickets_[index];
      auto expected = TicketPhase::armed;
      if (ticket.phase.compare_exchange_strong(
              expected, TicketPhase::retired,
              std::memory_order_acq_rel, std::memory_order_acquire)) {
        auto state = ticket.state;
        auto* const gate_ticket = ticket.gate_ticket;
        ticket.gate_ticket = nullptr;
        ticket.state.reset();
        state->record_drop_once(ticket);
        state->gate.settle(gate_ticket);
      } else if (expected == TicketPhase::callback_claimed) {
        callback_in_flight = true;
      }
    }
    return callback_in_flight;
  }

  static void output_callback(void*, void* source_frame_refcon,
                              OSStatus status,
                              VTEncodeInfoFlags info_flags,
                              CMSampleBufferRef sample) noexcept {
    auto* ticket = static_cast<CallbackTicket*>(source_frame_refcon);
    if (ticket == nullptr || !claim_ticket(*ticket)) {
      return;
    }
    auto state = ticket->state;
    state->handle_native(*ticket, status, info_flags, sample);
  }

  static void injected_callback(
      void*, void* source_frame_refcon,
      const MetalEncoderInjectedOutput& output) noexcept {
    auto* ticket = static_cast<CallbackTicket*>(source_frame_refcon);
    if (ticket == nullptr || !claim_ticket(*ticket)) {
      return;
    }
    auto state = ticket->state;
    state->handle_injected(*ticket, output);
  }

  void create_production_session(std::uint32_t width,
                                 std::uint32_t height) {
    const void* keys[] = {
        kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder,
        kVTVideoEncoderSpecification_EnableHardwareAcceleratedVideoEncoder};
    const void* values[] = {kCFBooleanTrue, kCFBooleanTrue};
    CFDictionaryRef specification = CFDictionaryCreate(
        kCFAllocatorDefault, keys, values, 2,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    if (specification == nullptr) {
      throw std::runtime_error("cannot create HEVC encoder specification");
    }
    VTCompressionSessionRef session = nullptr;
    const auto create_status = VTCompressionSessionCreate(
        kCFAllocatorDefault, static_cast<int32_t>(width),
        static_cast<int32_t>(height), kCMVideoCodecType_HEVC, specification,
        nullptr, nullptr, &Impl::output_callback, nullptr, &session);
    CFRelease(specification);
    require_status(create_status, "VTCompressionSessionCreate");
    try {
      configure_session(session);
    } catch (...) {
      VTCompressionSessionInvalidate(session);
      CFRelease(session);
      throw;
    }
    state_->using_hardware.store(true, std::memory_order_relaxed);
    session_ = std::make_shared<SessionHandle>(MetalEncoderNativeSession{
        session, &Impl::vt_encode, &Impl::vt_complete_frames,
        &Impl::vt_invalidate, &Impl::vt_release, true});
  }

  static void configure_session(VTCompressionSessionRef session) {
    require_status(VTSessionSetProperty(
                       session, kVTCompressionPropertyKey_RealTime,
                       kCFBooleanTrue),
                   "set HEVC realtime mode");
    require_status(VTSessionSetProperty(
                       session,
                       kVTCompressionPropertyKey_AllowFrameReordering,
                       kCFBooleanFalse),
                   "disable HEVC frame reordering");
    require_status(VTSessionSetProperty(
                       session, kVTCompressionPropertyKey_ProfileLevel,
                       kVTProfileLevel_HEVC_Main_AutoLevel),
                   "set HEVC profile");
    require_status(VTSessionSetProperty(
                       session, kVTCompressionPropertyKey_ExpectedFrameRate,
                       (__bridge CFTypeRef)@(30000.0 / 1001.0)),
                   "set HEVC expected frame rate");
    require_status(VTSessionSetProperty(
                       session, kVTCompressionPropertyKey_AverageBitRate,
                       (__bridge CFTypeRef)@(60'000'000)),
                   "set HEVC average bitrate");
    require_status(VTSessionSetProperty(
                       session,
                       kVTCompressionPropertyKey_MaxKeyFrameInterval,
                       (__bridge CFTypeRef)@(60)),
                   "set HEVC keyframe interval");
    require_status(VTCompressionSessionPrepareToEncodeFrames(session),
                   "prepare HEVC encoder");
    CFTypeRef hardware = nullptr;
    require_status(VTSessionCopyProperty(
                       session,
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
  }

  void flush_metrics() noexcept {
    if (metrics_ == nullptr) {
      return;
    }
    auto& metrics = *metrics_;
    const auto& state = *state_;
    metrics.encode_submissions.fetch_add(
        state.submissions.load(std::memory_order_relaxed),
        std::memory_order_relaxed);
    metrics.encode_completions.fetch_add(
        state.completions.load(std::memory_order_relaxed),
        std::memory_order_relaxed);
    metrics.encode_bytes.fetch_add(
        state.bytes.load(std::memory_order_relaxed),
        std::memory_order_relaxed);
    metrics.encode_drops.fetch_add(
        state.drops.load(std::memory_order_relaxed),
        std::memory_order_relaxed);
    metrics.encode_rejected_frames.fetch_add(
        state.rejected_frames.load(std::memory_order_relaxed),
        std::memory_order_relaxed);
    metrics.encode_callback_errors.fetch_add(
        state.callback_errors.load(std::memory_order_relaxed),
        std::memory_order_relaxed);
    metrics.encode_first_submit_ns.store(
        state.first_submit_ns.load(std::memory_order_relaxed),
        std::memory_order_relaxed);
    metrics.encode_last_completion_ns.store(
        state.last_completion_ns.load(std::memory_order_relaxed),
        std::memory_order_relaxed);
    metrics.encode_input_capacity.store(state.gate.capacity(),
                                        std::memory_order_relaxed);
    metrics.encode_input_in_use.store(state.gate.in_use(),
                                      std::memory_order_relaxed);
    metrics.encode_input_high_water.store(state.gate.high_water(),
                                          std::memory_order_relaxed);
    metrics.encode_input_pool_misses.store(state.gate.misses(),
                                           std::memory_order_relaxed);
    metrics.encode_using_hardware.store(
        state.using_hardware.load(std::memory_order_relaxed) ? 1U : 0U,
        std::memory_order_relaxed);
    metrics.encode_drain_timeouts.fetch_add(
        state.drain_timed_out.load(std::memory_order_relaxed) ? 1U : 0U,
        std::memory_order_relaxed);
    metrics_ = nullptr;
  }

  std::shared_ptr<State> state_;
  std::unique_ptr<CallbackTicket[]> tickets_;
  std::shared_ptr<SessionHandle> session_;
  swim::core::RuntimeCounters* metrics_{};
  std::chrono::milliseconds drain_timeout_;
  std::atomic_bool closed_{};
};

MetalEncoder::MetalEncoder(std::uint32_t width, std::uint32_t height,
                           const swim::core::AppConfig& config,
                           swim::core::RuntimeCounters& metrics)
    : impl_(std::make_shared<Impl>(width, height, config, metrics)) {}

MetalEncoder::MetalEncoder(std::uint32_t width, std::uint32_t height,
                           const swim::core::AppConfig& config,
                           swim::core::RuntimeCounters& metrics,
                           MetalEncoderDependencies dependencies)
    : impl_(std::make_shared<Impl>(width, height, config, metrics,
                                   std::move(dependencies))) {}

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
