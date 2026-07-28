#include <swim/core/benchmark_reporter.hpp>

#include <swim/core/build_info.hpp>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <fcntl.h>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <string_view>

#if defined(_WIN32)
// Windows provides the low-level descriptor calls under an underscore prefix in
// <io.h> and reports host/memory identity through the Win32 API instead of the
// POSIX headers. The rest of this file uses the swim_os_* shims below so the
// serialization and write logic stays platform-neutral.
#include <io.h>
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <psapi.h>
#else
#include <sys/utsname.h>
#include <unistd.h>
#endif

#if defined(__APPLE__)
#include <libproc.h>
#endif

namespace swim::core {
namespace {

#if defined(_WIN32)
constexpr int kStdoutFileno = 1;
int swim_os_write(int fd, const void* bytes, unsigned int size) {
  return _write(fd, bytes, size);
}
int swim_os_close(int fd) { return _close(fd); }
int swim_os_dup(int fd) { return _dup(fd); }
long long swim_os_seek_end(int fd) { return _lseeki64(fd, 0, SEEK_END); }
int swim_os_truncate(int fd, long long length) {
  return _chsize_s(fd, length);
}
int swim_os_open_append(const std::filesystem::path& path) {
  int fd = -1;
  // _wsopen_s preserves the wide native path and opens in binary append mode so
  // the JSONL records are written byte-for-byte like the POSIX O_APPEND path.
  const auto error = _wsopen_s(&fd, path.wstring().c_str(),
                               _O_WRONLY | _O_CREAT | _O_APPEND | _O_BINARY,
                               _SH_DENYNO, _S_IREAD | _S_IWRITE);
  if (error != 0) {
    return -1;
  }
  return fd;
}
#else
constexpr int kStdoutFileno = STDOUT_FILENO;
int swim_os_write(int fd, const void* bytes, std::size_t size) {
  return static_cast<int>(::write(fd, bytes, size));
}
int swim_os_close(int fd) { return ::close(fd); }
int swim_os_dup(int fd) { return ::dup(fd); }
long long swim_os_seek_end(int fd) {
  return static_cast<long long>(::lseek(fd, 0, SEEK_END));
}
int swim_os_truncate(int fd, long long length) {
  return ::ftruncate(fd, static_cast<off_t>(length));
}
int swim_os_open_append(const std::filesystem::path& path) {
  return open(path.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0644);
}
#endif

std::ptrdiff_t system_write(int fd, const void* bytes,
                            std::size_t size) {
  return static_cast<std::ptrdiff_t>(
      swim_os_write(fd, bytes, static_cast<unsigned int>(size)));
}

std::string json_escape(std::string_view value) {
  std::string escaped;
  escaped.reserve(value.size() + 2);
  for (const auto raw : value) {
    const auto character = static_cast<unsigned char>(raw);
    switch (character) {
      case '"':
        escaped += "\\\"";
        break;
      case '\\':
        escaped += "\\\\";
        break;
      case '\b':
        escaped += "\\b";
        break;
      case '\f':
        escaped += "\\f";
        break;
      case '\n':
        escaped += "\\n";
        break;
      case '\r':
        escaped += "\\r";
        break;
      case '\t':
        escaped += "\\t";
        break;
      default:
        if (character < 0x20) {
          constexpr char digits[] = "0123456789abcdef";
          escaped += "\\u00";
          escaped.push_back(digits[(character >> 4U) & 0x0fU]);
          escaped.push_back(digits[character & 0x0fU]);
        } else {
          escaped.push_back(static_cast<char>(character));
        }
    }
  }
  return escaped;
}

void append_string(std::ostringstream& output, std::string_view key,
                   std::string_view value) {
  output << ",\"" << key << "\":\"" << json_escape(value) << '"';
}

// Emits only `count` leading lanes so the JSON array length tracks the run's
// active camera count rather than the compile-time capacity.
template <std::size_t Size>
void append_array(std::ostringstream& output, std::string_view key,
                  const std::array<std::uint64_t, Size>& values,
                  std::size_t count) {
  output << ",\"" << key << "\":[";
  for (std::size_t index = 0; index < std::min(count, Size); ++index) {
    if (index != 0) {
      output << ',';
    }
    output << values[index];
  }
  output << ']';
}

template <std::size_t Size>
std::array<std::uint64_t, Size> interval_array(
    const std::array<std::uint64_t, Size>& current,
    const std::array<std::uint64_t, Size>& previous,
    bool final) noexcept {
  if (final) {
    return current;
  }
  std::array<std::uint64_t, Size> delta{};
  for (std::size_t index = 0; index < Size; ++index) {
    delta[index] = monotonic_delta(current[index], previous[index]);
  }
  return delta;
}

std::uint64_t event_value(std::uint64_t current, std::uint64_t previous,
                          bool final) noexcept {
  return final ? current : monotonic_delta(current, previous);
}

double rate(std::uint64_t count, double seconds) noexcept {
  return seconds > 0.0 && std::isfinite(seconds)
             ? static_cast<double>(count) / seconds
             : 0.0;
}

std::string default_run_id() {
  const auto now = std::chrono::system_clock::now();
  const auto timestamp = std::chrono::system_clock::to_time_t(now);
  std::tm utc{};
#if defined(_WIN32)
  gmtime_s(&utc, &timestamp);
#else
  gmtime_r(&timestamp, &utc);
#endif
  char buffer[32]{};
  static_cast<void>(std::strftime(buffer, sizeof(buffer), "%Y%m%dT%H%M%SZ",
                                  &utc));
  const auto short_length = std::min<std::size_t>(12, build_info::git_sha.size());
  return std::string(buffer) + "-" +
         std::string(build_info::git_sha.substr(0, short_length));
}

std::optional<std::uint64_t> resident_bytes() noexcept {
#if defined(__APPLE__)
  rusage_info_v2 info{};
  if (proc_pid_rusage(getpid(), RUSAGE_INFO_V2,
                      reinterpret_cast<rusage_info_t*>(&info)) == 0) {
    return info.ri_resident_size;
  }
#elif defined(_WIN32)
  PROCESS_MEMORY_COUNTERS counters{};
  counters.cb = sizeof(counters);
  if (GetProcessMemoryInfo(GetCurrentProcess(), &counters,
                           sizeof(counters)) != 0) {
    return static_cast<std::uint64_t>(counters.WorkingSetSize);
  }
#endif
  return std::nullopt;
}

std::array<std::string, 3> machine_identity() {
#if defined(_WIN32)
  char hostname[MAX_COMPUTERNAME_LENGTH + 1] = "unavailable";
  DWORD length = sizeof(hostname);
  if (GetComputerNameA(hostname, &length) == 0) {
    return {"unavailable", "unavailable", "unavailable"};
  }
  std::string machine = "unknown";
  SYSTEM_INFO system_info{};
  GetNativeSystemInfo(&system_info);
  switch (system_info.wProcessorArchitecture) {
    case PROCESSOR_ARCHITECTURE_AMD64:
      machine = "x86_64";
      break;
    case PROCESSOR_ARCHITECTURE_ARM64:
      machine = "arm64";
      break;
    case PROCESSOR_ARCHITECTURE_INTEL:
      machine = "x86";
      break;
    default:
      machine = "unknown";
      break;
  }
  return {std::string(hostname), "Windows", machine};
#else
  utsname value{};
  if (uname(&value) != 0) {
    return {"unavailable", "unavailable", "unavailable"};
  }
  return {value.nodename, std::string(value.sysname) + " " + value.release,
          value.machine};
#endif
}

int open_output(const std::filesystem::path& path) {
  if (path.empty()) {
    const auto duplicate = swim_os_dup(kStdoutFileno);
    if (duplicate < 0) {
      throw std::runtime_error("cannot duplicate benchmark stdout descriptor");
    }
    return duplicate;
  }
  const auto parent = path.parent_path();
  if (!parent.empty()) {
    std::filesystem::create_directories(parent);
  }
  const auto fd = swim_os_open_append(path);
  if (fd < 0) {
    throw std::runtime_error("cannot open metrics output: " + path.string());
  }
  return fd;
}

}  // namespace

std::string serialize_benchmark_record(
    const BenchmarkReporterMetadata& metadata,
    const MetricsSnapshot& current,
    const MetricsSnapshot& previous,
    BackendRuntimeSample backend,
    double elapsed_seconds,
    bool final,
    std::size_t healthy_sources,
    std::optional<std::uint64_t> rss_bytes) {
  const auto render_completions = event_value(
      current.render_completions, previous.render_completions, final);
  const auto preview_completions = event_value(
      current.preview_completions, previous.preview_completions, final);
  const auto encode_completions = event_value(
      current.encode_completions, previous.encode_completions, final);
  const auto render_fps = final ? current.render_completion_fps()
                                : rate(render_completions, elapsed_seconds);
  const auto encode_fps = final ? current.encode_completion_fps()
                                : rate(encode_completions, elapsed_seconds);
  const auto machine = machine_identity();
  // Per-camera arrays report the lanes this run actually drives.
  const auto lane_count =
      static_cast<std::size_t>(metadata.graph.active_sources);
  auto frame_age_p50 = current.frame_age_ms_p50;
  auto frame_age_p95 = current.frame_age_ms_p95;
  auto frame_age_p99 = current.frame_age_ms_p99;
  auto age_spread_p99 = current.snapshot_age_spread_ms_p99;
  auto gpu_p50 = current.gpu_render_duration_ms_p50;
  auto gpu_p95 = current.gpu_render_duration_ms_p95;
  if (!final) {
    for (std::size_t camera = 0; camera < frame_age_p50.size(); ++camera) {
      const auto histogram = current.frame_age_histograms[camera].delta_from(
          previous.frame_age_histograms[camera]);
      frame_age_p50[camera] = static_cast<std::uint64_t>(
          histogram.percentile(0.50).count());
      frame_age_p95[camera] = static_cast<std::uint64_t>(
          histogram.percentile(0.95).count());
      frame_age_p99[camera] = static_cast<std::uint64_t>(
          histogram.percentile(0.99).count());
    }
    const auto spread = current.snapshot_age_spread_histogram.delta_from(
        previous.snapshot_age_spread_histogram);
    age_spread_p99 =
        static_cast<std::uint64_t>(spread.percentile(0.99).count());
    const auto gpu = current.gpu_render_duration_histogram.delta_from(
        previous.gpu_render_duration_histogram);
    gpu_p50 = static_cast<std::uint64_t>(gpu.percentile(0.50).count());
    gpu_p95 = static_cast<std::uint64_t>(gpu.percentile(0.95).count());
  }
  const auto run_id = metadata.manifest.has_value()
                          ? metadata.manifest->run_id
                          : metadata.generated_run_id;
  if (run_id.empty()) {
    throw std::runtime_error("benchmark record requires a run_id");
  }

  std::ostringstream line;
  line << std::fixed << std::setprecision(3)
       << "{\"schema\":1";
  append_string(line, "run_id", run_id);
  line << ",\"final\":" << (final ? "true" : "false");
  append_string(line, "backend", metadata.config.backend);
  append_string(line, "mode", pacing_name(metadata.config.mode));
  append_string(line, "stage", benchmark_stage_name(metadata.config.stage));
  append_string(line, "pacing",
                metadata.config.mode == RunMode::realtime ? "paced"
                                                          : "unpaced");
  append_string(line, "build_type", build_info::build_type);
  append_string(line, "compiler", build_info::compiler);
  append_string(line, "git_sha", build_info::git_sha);
  line << ",\"stream_count\":" << metadata.config.stream_count
       << ",\"elapsed_s\":" << elapsed_seconds
       << ",\"render_fps\":" << render_fps
       << ",\"preview_fps\":" << rate(preview_completions, elapsed_seconds)
       << ",\"encode_fps\":" << encode_fps
       << ",\"gpu_render_ms_p50\":"
       << gpu_p50
       << ",\"gpu_render_ms_p95\":"
       << gpu_p95;
  append_array(line, "frame_age_ms_p50", frame_age_p50, lane_count);
  append_array(line, "frame_age_ms_p95", frame_age_p95, lane_count);
  append_array(line, "frame_age_ms_p99", frame_age_p99, lane_count);
  line << ",\"snapshot_age_spread_ms_p99\":"
       << age_spread_p99;
  append_array(line, "camera_received",
               interval_array(current.camera_received,
                              previous.camera_received, final), lane_count);
  append_array(line, "camera_decoded",
               interval_array(current.camera_decoded,
                              previous.camera_decoded, final), lane_count);
  append_array(line, "camera_published",
               interval_array(current.camera_published,
                              previous.camera_published, final), lane_count);
  append_array(line, "mailbox_overwrites",
               interval_array(current.camera_overwritten,
                              previous.camera_overwritten, final), lane_count);
  append_array(line, "frame_reuses",
               interval_array(current.camera_reused,
                              previous.camera_reused, final), lane_count);

  line << ",\"received\":"
       << event_value(current.received, previous.received, final)
       << ",\"decoded\":"
       << event_value(current.decoded, previous.decoded, final)
       << ",\"published\":"
       << event_value(current.published, previous.published, final)
       << ",\"overwritten\":"
       << event_value(current.overwritten, previous.overwritten, final)
       << ",\"reused\":"
       << event_value(current.reused, previous.reused, final)
       << ",\"malformed\":"
       << event_value(current.malformed, previous.malformed, final)
       << ",\"reconnects\":"
       << event_value(current.reconnects, previous.reconnects, final)
       << ",\"render_submissions\":"
       << event_value(current.render_submissions,
                      previous.render_submissions, final)
       << ",\"render_completions\":" << render_completions
       << ",\"render_drops\":"
       << event_value(current.render_drops, previous.render_drops, final)
       << ",\"render_active_ns\":" << current.render_active_ns
       << ",\"render_first_submit_ns\":" << current.render_first_submit_ns
       << ",\"render_last_completion_ns\":"
       << current.render_last_completion_ns
       << ",\"render_completion_interval_ns\":"
       << current.render_completion_interval_ns()
       << ",\"render_inflight_capacity\":"
       << current.render_inflight_capacity
       << ",\"render_inflight_in_use\":"
       << current.render_inflight_in_use
       << ",\"render_inflight_high_water\":"
       << current.render_inflight_high_water
       << ",\"render_inflight_pool_misses\":"
       << event_value(current.render_inflight_pool_misses,
                      previous.render_inflight_pool_misses, final)
       << ",\"render_output_capacity\":"
       << current.render_output_capacity
       << ",\"render_output_in_use\":" << current.render_output_in_use
       << ",\"render_output_high_water\":"
       << current.render_output_high_water
       << ",\"render_output_pool_misses\":"
       << event_value(current.render_output_pool_misses,
                      previous.render_output_pool_misses, final)
       << ",\"preview_submissions\":"
       << event_value(current.preview_submissions,
                      previous.preview_submissions, final)
       << ",\"preview_completions\":" << preview_completions
       << ",\"preview_drops\":"
       << event_value(current.preview_drops, previous.preview_drops, final)
       << ",\"preview_presents\":"
       << event_value(current.preview_presents,
                      previous.preview_presents, final)
       << ",\"encode_submissions\":"
       << event_value(current.encode_submissions,
                      previous.encode_submissions, final)
       << ",\"encode_completions\":" << encode_completions
       << ",\"encode_bytes\":"
       << event_value(current.encode_bytes, previous.encode_bytes, final)
       << ",\"encode_drops\":"
       << event_value(current.encode_drops, previous.encode_drops, final)
       << ",\"encode_rejected_frames\":"
       << event_value(current.encode_rejected_frames,
                      previous.encode_rejected_frames, final)
       << ",\"encode_callback_errors\":"
       << event_value(current.encode_callback_errors,
                      previous.encode_callback_errors, final)
       << ",\"encode_first_submit_ns\":" << current.encode_first_submit_ns
       << ",\"encode_last_completion_ns\":"
       << current.encode_last_completion_ns
       << ",\"encode_input_capacity\":" << current.encode_input_capacity
       << ",\"encode_input_in_use\":" << current.encode_input_in_use
       << ",\"encode_input_high_water\":"
       << current.encode_input_high_water
       << ",\"encode_input_pool_misses\":"
       << event_value(current.encode_input_pool_misses,
                      previous.encode_input_pool_misses, final)
       << ",\"encode_using_hardware\":"
       << (current.encode_using_hardware ? "true" : "false")
       << ",\"encode_drain_timeouts\":"
       << event_value(current.encode_drain_timeouts,
                      previous.encode_drain_timeouts, final)
       << ",\"encode_codec\":\"hevc\""
       << ",\"pool_exhaustion\":"
       << event_value(current.pool_exhaustion, previous.pool_exhaustion,
                      final)
       << ",\"decoded_pixel_host_copies\":"
       << event_value(current.decoded_pixel_host_copies,
                      previous.decoded_pixel_host_copies, final);

  append_array(line, "decode_surface_pool_capacity",
               current.decode_surface_capacity, lane_count);
  append_array(line, "decode_surface_pool_in_use",
               current.decode_surface_in_use, lane_count);
  append_array(line, "decode_surface_pool_high_water",
               current.decode_surface_high_water, lane_count);
  append_array(line, "decode_surface_pool_misses",
               interval_array(current.decode_surface_pool_misses,
                              previous.decode_surface_pool_misses, final), lane_count);
  append_array(line, "decode_ticket_pool_capacity",
               current.decode_ticket_capacity, lane_count);
  append_array(line, "decode_ticket_pool_in_use",
               current.decode_ticket_in_use, lane_count);
  append_array(line, "decode_ticket_pool_high_water",
               current.decode_ticket_high_water, lane_count);
  append_array(line, "decode_ticket_pool_misses",
               interval_array(current.decode_ticket_pool_misses,
                              previous.decode_ticket_pool_misses, final), lane_count);

  const auto texture_wrappers = event_value(
      current.native_texture_wrappers, previous.native_texture_wrappers,
      final);
  const auto command_buffers = event_value(
      current.native_command_buffers, previous.native_command_buffers,
      final);
  const auto decode_tickets = event_value(
      current.native_decode_tickets, previous.native_decode_tickets, final);
  line << ",\"native_texture_wrappers\":" << texture_wrappers
       << ",\"native_command_buffers\":" << command_buffers
       << ",\"native_decode_tickets\":" << decode_tickets
       << ",\"native_callback_wrappers\":"
       << event_value(current.native_callback_wrappers,
                      previous.native_callback_wrappers, final)
       << ",\"application_owned_frame_allocations\":"
       << event_value(current.application_hot_path_allocations,
                      previous.application_hot_path_allocations, final)
       << ",\"native_wrapper_creations\":{"
       << "\"cv_metal_texture\":" << texture_wrappers
       << ",\"metal_command_buffer\":" << command_buffers
       << ",\"videotoolbox_ticket\":" << decode_tickets << '}'
       << ",\"rss_bytes\":";
  if (rss_bytes.has_value()) {
    line << *rss_bytes;
  } else {
    line << "null";
  }
  line << ",\"gpu_allocated_bytes\":";
  if (backend.gpu_allocated_bytes.has_value()) {
    line << *backend.gpu_allocated_bytes;
  } else {
    line << "null";
  }
  line << ",\"sources_healthy\":" << healthy_sources
       << ",\"output_width\":" << metadata.output_width
       << ",\"output_height\":" << metadata.output_height;

  append_string(line, "requested_stage",
                benchmark_stage_name(metadata.config.stage));
  append_string(line, "requested_pacing", pacing_name(metadata.config.mode));
  line << ",\"requested_stream_count\":" << metadata.config.stream_count
       << ",\"requested_preview\":"
       << (metadata.config.preview ? "true" : "false")
       << ",\"requested_encode\":"
       << (metadata.config.encode ? "true" : "false")
       << ",\"resolved_active_sources\":" << metadata.graph.active_sources
       << ",\"resolved_create_renderer\":"
       << (metadata.graph.create_renderer ? "true" : "false")
       << ",\"resolved_synthetic_inputs\":"
       << (metadata.graph.synthetic_inputs ? "true" : "false")
       << ",\"resolved_preview\":"
       << (metadata.graph.preview ? "true" : "false")
       << ",\"resolved_encode\":"
       << (metadata.graph.encode ? "true" : "false")
       << ",\"resolved_graph\":{"
       << "\"active_sources\":" << metadata.graph.active_sources
       << ",\"create_renderer\":"
       << (metadata.graph.create_renderer ? "true" : "false")
       << ",\"synthetic_inputs\":"
       << (metadata.graph.synthetic_inputs ? "true" : "false")
       << ",\"preview\":"
       << (metadata.graph.preview ? "true" : "false")
       << ",\"encode\":"
       << (metadata.graph.encode ? "true" : "false") << '}'
       << ",\"resolved_config\":{"
       << "\"fps_num\":" << metadata.config.fps_num
       << ",\"fps_den\":" << metadata.config.fps_den
       << ",\"decode_surface_pool\":"
       << metadata.config.decode_surface_pool
       << ",\"decode_ticket_pool\":"
       << metadata.config.decode_ticket_pool
       << ",\"render_inflight\":" << metadata.config.render_inflight
       << ",\"output_pool\":" << metadata.config.output_pool << '}';

  if (metadata.manifest.has_value()) {
    line << ",\"fingerprints_verified\":true";
    append_string(line, "asset_sha256", metadata.manifest->asset_sha256);
    line << ",\"source_sha256\":[";
    for (std::size_t index = 0;
         index < metadata.manifest->source_sha256.size(); ++index) {
      if (index != 0) {
        line << ',';
      }
      line << '"' << json_escape(metadata.manifest->source_sha256[index])
           << '"';
    }
    line << ']';
  } else {
    line << ",\"fingerprints_verified\":false"
         << ",\"asset_sha256\":null"
         << ",\"source_sha256\":[null,null,null,null,null,null]";
  }
  line << ",\"machine\":{";
  line << "\"hostname\":\"" << json_escape(machine[0]) << '"'
       << ",\"os\":\"" << json_escape(machine[1]) << '"'
       << ",\"arch\":\"" << json_escape(machine[2]) << '"' << "}}\n";
  return line.str();
}

BenchmarkReporter::BenchmarkReporter(BenchmarkReporterMetadata metadata,
                                     RuntimeCounters& metrics,
                                     BenchmarkWriteOperation writer)
    : metadata_(std::move(metadata)),
      metrics_(metrics),
      writer_(writer ? std::move(writer)
                     : BenchmarkWriteOperation{system_write}),
      output_fd_(open_output(metadata_.config.metrics_path)),
      started_at_(std::chrono::steady_clock::now()),
      previous_at_(started_at_) {
  if (!metadata_.manifest.has_value() && metadata_.generated_run_id.empty()) {
    metadata_.generated_run_id = default_run_id();
  }
  previous_.emplace(metrics_.sample_totals());
}

BenchmarkReporter::~BenchmarkReporter() {
  join_intervals();
  // The backend is borrowed and may already have been destroyed during stack
  // unwinding. Destructor fallback records therefore use unavailable/null GPU
  // telemetry instead of sampling a possibly stale object.
  backend_ = nullptr;
  if (!final_attempted_) {
    try {
      write_final(0);
    } catch (...) {
    }
  }
  if (output_fd_ >= 0) {
    static_cast<void>(swim_os_close(output_fd_));
  }
}

void BenchmarkReporter::bind_backend(const IBackend& backend) noexcept {
  backend_ = &backend;
}

void BenchmarkReporter::unbind_backend() noexcept { backend_ = nullptr; }

void BenchmarkReporter::start() {
  if (started_) {
    throw std::logic_error("benchmark reporter already started");
  }
  started_ = true;
  thread_ = std::jthread([this](std::stop_token token) {
    auto deadline = std::chrono::steady_clock::now() +
                    std::chrono::seconds{1};
    std::unique_lock lock(wait_mutex_);
    while (!token.stop_requested()) {
      const bool stopped = wait_condition_.wait_until(
          lock, token, deadline, [] { return false; });
      if (stopped || token.stop_requested()) {
        return;
      }
      lock.unlock();
      try {
        write_interval_now();
      } catch (...) {
        remember_background_error(std::current_exception());
        return;
      }
      lock.lock();
      deadline += std::chrono::seconds{1};
    }
  });
}

void BenchmarkReporter::join_intervals() noexcept {
  if (thread_.joinable()) {
    thread_.request_stop();
    wait_condition_.notify_all();
    thread_.join();
  }
}

void BenchmarkReporter::stop_intervals() {
  join_intervals();
  if (const auto error = background_error()) {
    std::rethrow_exception(error);
  }
}

void BenchmarkReporter::write_interval_now() {
  if (final_attempted_) {
    throw std::logic_error("benchmark final record already written");
  }
  if (const auto error = background_error()) {
    std::rethrow_exception(error);
  }
  const auto now = std::chrono::steady_clock::now();
  const auto current = metrics_.sample_totals();
  const auto backend = backend_ == nullptr ? BackendRuntimeSample{}
                                           : backend_->sample_runtime();
  const auto interval = std::chrono::duration<double>(now - previous_at_).count();
  write_record(current, *previous_, backend, interval, false, 0);
  previous_.emplace(current);
  previous_at_ = now;
}

void BenchmarkReporter::write_final(std::size_t healthy_sources) {
  if (final_attempted_) {
    return;
  }
  join_intervals();
  final_attempted_ = true;
  if (const auto error = background_error()) {
    std::rethrow_exception(error);
  }
  const auto now = std::chrono::steady_clock::now();
  const auto current = metrics_.sample_totals();
  const auto backend = backend_ == nullptr ? BackendRuntimeSample{}
                                           : backend_->sample_runtime();
  const auto elapsed = std::chrono::duration<double>(now - started_at_).count();
  write_record(current, *previous_, backend, elapsed, true,
               healthy_sources);
  if (const auto error = background_error()) {
    std::rethrow_exception(error);
  }
}

void BenchmarkReporter::remember_background_error(
    std::exception_ptr error) noexcept {
  std::lock_guard lock(error_mutex_);
  if (!background_error_) {
    background_error_ = std::move(error);
  }
}

std::exception_ptr BenchmarkReporter::background_error() const noexcept {
  std::lock_guard lock(error_mutex_);
  return background_error_;
}

void BenchmarkReporter::write_record(const MetricsSnapshot& current,
                                     const MetricsSnapshot& previous,
                                     BackendRuntimeSample backend,
                                     double elapsed_seconds,
                                     bool final,
                                     std::size_t healthy_sources) {
  const auto line = serialize_benchmark_record(
      metadata_, current, previous, backend, elapsed_seconds, final,
      healthy_sources, resident_bytes());
  const auto record_start = swim_os_seek_end(output_fd_);
  const auto written = writer_(output_fd_, line.data(), line.size());
  if (written < 0) {
    const auto write_error = errno;
    if (record_start >= 0) {
      static_cast<void>(swim_os_truncate(output_fd_, record_start));
    }
    static_cast<void>(swim_os_close(output_fd_));
    output_fd_ = -1;
    auto error = std::make_exception_ptr(std::runtime_error(
        "benchmark metrics write failed: " + std::to_string(write_error)));
    remember_background_error(error);
    std::rethrow_exception(error);
  }
  if (static_cast<std::size_t>(written) != line.size()) {
    if (record_start >= 0) {
      static_cast<void>(swim_os_truncate(output_fd_, record_start));
    }
    static_cast<void>(swim_os_close(output_fd_));
    output_fd_ = -1;
    auto error = std::make_exception_ptr(
        std::runtime_error("benchmark metrics write was partial"));
    remember_background_error(error);
    std::rethrow_exception(error);
  }
}

}  // namespace swim::core
