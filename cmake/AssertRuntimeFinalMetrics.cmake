set(metrics_path "/tmp/swim_runtime_setup_failure.jsonl")
file(REMOVE "${metrics_path}")
execute_process(
  COMMAND "${SWIM_REALTIME}" --config
          cpp/tests/fixtures/runtime_unknown_backend.conf
          "--metrics=${metrics_path}"
  WORKING_DIRECTORY "${REPOSITORY_ROOT}"
  RESULT_VARIABLE result
  OUTPUT_VARIABLE output
  ERROR_VARIABLE error)

if(result EQUAL 0)
  message(FATAL_ERROR "runtime setup failure unexpectedly exited zero")
endif()
if(NOT EXISTS "${metrics_path}")
  message(FATAL_ERROR "runtime setup failure created no metrics file")
endif()
file(STRINGS "${metrics_path}" records)
list(LENGTH records record_count)
if(NOT record_count EQUAL 1)
  message(FATAL_ERROR "runtime setup failure emitted ${record_count} records")
endif()
list(GET records 0 output)
file(READ "${metrics_path}" raw_output)
if(NOT raw_output STREQUAL "${output}\n")
  message(FATAL_ERROR "runtime setup record must have exactly one newline")
endif()
if(NOT output MATCHES "\\\"schema\\\":1")
  message(FATAL_ERROR "runtime setup failure omitted schema 1: ${output}")
endif()
if(NOT output MATCHES "\\\"final\\\":true")
  message(FATAL_ERROR "runtime setup failure emitted no final JSON: ${output}")
endif()
if(NOT output MATCHES "\\\"sources_healthy\\\":0")
  message(FATAL_ERROR "unstarted sources were reported healthy: ${output}")
endif()
if(NOT output MATCHES "\\\"preview_drops\\\":0")
  message(FATAL_ERROR "final JSON omitted preview drops: ${output}")
endif()
if(NOT output MATCHES "\\\"preview_presents\\\":0")
  message(FATAL_ERROR "final JSON omitted preview presents: ${output}")
endif()
foreach(field IN ITEMS encode_submissions encode_completions encode_bytes
                       encode_drops encode_rejected_frames encode_callback_errors
                       encode_first_submit_ns encode_last_completion_ns
                       encode_input_capacity encode_input_in_use
                       encode_input_high_water encode_input_pool_misses
                       encode_drain_timeouts)
  if(NOT output MATCHES "\\\"${field}\\\":0")
    message(FATAL_ERROR "final JSON omitted zero ${field}: ${output}")
  endif()
endforeach()
if(NOT output MATCHES "\\\"encode_fps\\\":0\\.000")
  message(FATAL_ERROR "final JSON omitted zero encode_fps: ${output}")
endif()
if(NOT output MATCHES "\\\"encode_using_hardware\\\":false")
  message(FATAL_ERROR "final JSON omitted false hardware flag: ${output}")
endif()
if(NOT output MATCHES "\\\"encode_codec\\\":\\\"hevc\\\"")
  message(FATAL_ERROR "final JSON omitted HEVC codec: ${output}")
endif()
if(NOT error MATCHES "unknown backend 'missing-runtime-backend'")
  message(FATAL_ERROR "unexpected setup error: ${error}")
endif()
