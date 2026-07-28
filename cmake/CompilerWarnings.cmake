function(swim_enable_warnings target)
  if(MSVC)
    target_compile_options(${target} PRIVATE
      /W4
      /permissive-)
    # Media Foundation and Direct3D headers, and the JSONL serialization, rely
    # on standard C/C++ APIs MSVC flags as "insecure"; silence those blanket
    # deprecations rather than rewriting portable code per platform.
    target_compile_definitions(${target} PRIVATE
      _CRT_SECURE_NO_WARNINGS
      _CRT_NONSTDC_NO_WARNINGS)
  else()
    target_compile_options(${target} PRIVATE
      -Wall
      -Wextra
      -Wpedantic
      -Wconversion
      -Wshadow)
  endif()
endfunction()
