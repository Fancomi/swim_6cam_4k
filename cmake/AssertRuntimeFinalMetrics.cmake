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
if(NOT error MATCHES "unknown backend 'missing-runtime-backend'")
  message(FATAL_ERROR "unexpected setup error: ${error}")
endif()
