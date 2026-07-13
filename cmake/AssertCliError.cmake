if(NOT DEFINED SWIM_REALTIME OR NOT DEFINED REPOSITORY_ROOT OR
   NOT DEFINED CLI_CASE)
  message(FATAL_ERROR "CLI assertion requires executable, root, and case")
endif()

if(CLI_CASE STREQUAL "requires_config")
  set(arguments --validate-only)
  set(expected_error "error: missing required --config PATH")
elseif(CLI_CASE STREQUAL "missing_path")
  set(arguments --config)
  set(expected_error "error: --config requires PATH")
elseif(CLI_CASE STREQUAL "repeated_config")
  set(arguments
      --config cpp/tests/fixtures/cli.conf
      --config cpp/tests/fixtures/cli.conf
      --validate-only)
  set(expected_error "error: duplicate command-line option '--config'")
elseif(CLI_CASE STREQUAL "unknown_override")
  set(arguments --config cpp/tests/fixtures/cli.conf --wat=true)
  set(expected_error "error: unknown command-line option '--wat=true'")
else()
  message(FATAL_ERROR "unknown CLI assertion case: ${CLI_CASE}")
endif()

execute_process(
  COMMAND "${SWIM_REALTIME}" ${arguments}
  WORKING_DIRECTORY "${REPOSITORY_ROOT}"
  RESULT_VARIABLE actual_status
  OUTPUT_VARIABLE actual_stdout
  ERROR_VARIABLE actual_stderr)

string(REGEX REPLACE "[\r\n]+$" "" actual_stderr "${actual_stderr}")
if(NOT actual_status STREQUAL "1")
  message(FATAL_ERROR
    "${CLI_CASE}: expected status 1, got '${actual_status}'")
endif()
if(NOT actual_stdout STREQUAL "")
  message(FATAL_ERROR
    "${CLI_CASE}: expected empty stdout, got '${actual_stdout}'")
endif()
if(NOT actual_stderr STREQUAL expected_error)
  message(FATAL_ERROR
    "${CLI_CASE}: expected stderr '${expected_error}', got '${actual_stderr}'")
endif()
