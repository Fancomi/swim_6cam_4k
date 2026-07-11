execute_process(
  COMMAND "${SWIM_REALTIME}" --config
          cpp/tests/fixtures/runtime_unknown_backend.conf
          --metrics=/tmp/swim_runtime_setup_failure.jsonl
  WORKING_DIRECTORY "${REPOSITORY_ROOT}"
  RESULT_VARIABLE result
  OUTPUT_VARIABLE output
  ERROR_VARIABLE error)

if(result EQUAL 0)
  message(FATAL_ERROR "runtime setup failure unexpectedly exited zero")
endif()
if(NOT output MATCHES "\\\"final\\\":true")
  message(FATAL_ERROR "runtime setup failure emitted no final JSON: ${output}")
endif()
string(REGEX MATCHALL "\\\"final\\\":true" final_lines "${output}")
list(LENGTH final_lines final_line_count)
if(NOT final_line_count EQUAL 1)
  message(FATAL_ERROR "runtime setup failure emitted duplicate final JSON: ${output}")
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
