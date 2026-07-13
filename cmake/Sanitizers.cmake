set(SWIM_SANITIZER "" CACHE STRING
    "Comma-separated sanitizers (address, undefined, or thread)")

function(swim_enable_sanitizers target)
  if(NOT SWIM_SANITIZER)
    return()
  endif()

  if(NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    message(FATAL_ERROR
      "SWIM_SANITIZER requires a Clang- or GCC-compatible compiler")
  endif()

  set(supported_sanitizers address undefined thread)
  string(REPLACE "," ";" requested_sanitizers "${SWIM_SANITIZER}")
  foreach(sanitizer IN LISTS requested_sanitizers)
    if(NOT sanitizer IN_LIST supported_sanitizers)
      message(FATAL_ERROR "Unsupported SWIM_SANITIZER value: ${sanitizer}")
    endif()
  endforeach()

  target_compile_options(${target} PRIVATE
    "-fsanitize=${SWIM_SANITIZER}"
    -fno-omit-frame-pointer)
  target_link_options(${target} PRIVATE "-fsanitize=${SWIM_SANITIZER}")
endfunction()
