cmake_minimum_required(VERSION 3.25)

project(swim_realtime LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)

include(cmake/CompilerWarnings.cmake)
include(cmake/Sanitizers.cmake)

set(RUNTIME_ASSET ${CMAKE_SOURCE_DIR}/assets/generated/pool_4k.swasset)
add_custom_command(
  OUTPUT ${RUNTIME_ASSET}
  COMMAND ${Python3_EXECUTABLE} -m python.assets.compile_runtime_asset
          ${CMAKE_SOURCE_DIR}/outputs/data/pool_mesh.json ${RUNTIME_ASSET}
          --camera-ids cam3 cam2 cam1 cam4 cam5 cam6 --ppm 100
  WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  DEPENDS outputs/data/pool_mesh.json
          python/assets/asset_format.py
          python/assets/compile_runtime_asset.py
  VERBATIM)
add_custom_target(runtime_asset DEPENDS ${RUNTIME_ASSET})

add_library(swim_core
  cpp/core/src/asset.cpp
  cpp/core/src/backend.cpp
  cpp/core/src/camera_health.cpp
  cpp/core/src/config.cpp
  cpp/core/src/frame.cpp
  cpp/core/src/hot_path_allocations.cpp
  cpp/core/src/metrics.cpp
  cpp/core/src/runtime_validation.cpp)
target_include_directories(swim_core PUBLIC cpp/core/include)
swim_enable_warnings(swim_core)
swim_enable_sanitizers(swim_core)

add_executable(swim_realtime cpp/app/main.cpp)
target_link_libraries(swim_realtime PRIVATE swim_core)
swim_enable_warnings(swim_realtime)
swim_enable_sanitizers(swim_realtime)
add_dependencies(swim_realtime runtime_asset)

enable_testing()
add_executable(swim_core_tests
  cpp/tests/test_main.cpp
  cpp/tests/test_asset.cpp
  cpp/tests/test_backend.cpp
  cpp/tests/test_camera_health.cpp
  cpp/tests/test_config.cpp
  cpp/tests/test_frame_mailbox.cpp
  cpp/tests/test_metrics.cpp)
target_link_libraries(swim_core_tests PRIVATE swim_core)
target_compile_definitions(swim_core_tests PRIVATE
  SWIM_TEST_ASSET_PATH="${RUNTIME_ASSET}"
  SWIM_TEST_FIXTURE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/cpp/tests/fixtures")
swim_enable_warnings(swim_core_tests)
swim_enable_sanitizers(swim_core_tests)
add_dependencies(swim_core_tests runtime_asset)
add_test(NAME swim_core_tests COMMAND swim_core_tests)

add_test(
  NAME swim_validate_cli
  COMMAND swim_realtime --config cpp/tests/fixtures/cli.conf --validate-only)
set_tests_properties(swim_validate_cli PROPERTIES
  WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
  PASS_REGULAR_EXPRESSION
    "camera_order=cam3,cam2,cam1,cam4,cam5,cam6")

add_test(
  NAME swim_validate_cli_dimensions
  COMMAND swim_realtime --config cpp/tests/fixtures/cli.conf --validate-only)
set_tests_properties(swim_validate_cli_dimensions PROPERTIES
  WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
  PASS_REGULAR_EXPRESSION "dimensions=5001x2101 -> 5002x2102")

function(add_swim_cli_error_test test_name cli_case)
  add_test(
    NAME ${test_name}
    COMMAND ${CMAKE_COMMAND}
            "-DSWIM_REALTIME=$<TARGET_FILE:swim_realtime>"
            "-DREPOSITORY_ROOT=${CMAKE_CURRENT_SOURCE_DIR}"
            "-DCLI_CASE=${cli_case}"
            -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/AssertCliError.cmake")
endfunction()

add_swim_cli_error_test(swim_cli_requires_config requires_config)
add_swim_cli_error_test(swim_cli_rejects_missing_config_path missing_path)
add_swim_cli_error_test(swim_cli_rejects_repeated_config repeated_config)
add_swim_cli_error_test(swim_cli_rejects_unknown_override unknown_override)

if(APPLE)
  enable_language(OBJCXX)
  find_library(SWIM_METAL_FRAMEWORK Metal REQUIRED)
  find_library(SWIM_COREVIDEO_FRAMEWORK CoreVideo REQUIRED)
  find_library(SWIM_COREMEDIA_FRAMEWORK CoreMedia REQUIRED)
  find_library(SWIM_IMAGEIO_FRAMEWORK ImageIO REQUIRED)
  find_library(SWIM_FOUNDATION_FRAMEWORK Foundation REQUIRED)
  find_library(SWIM_IOSURFACE_FRAMEWORK IOSurface REQUIRED)
  find_library(SWIM_COREGRAPHICS_FRAMEWORK CoreGraphics REQUIRED)

  add_library(swim_metal_renderer
    cpp/backends/metal/src/metal_renderer.mm)
  target_include_directories(swim_metal_renderer PUBLIC
    cpp/backends/metal/include)
  target_compile_definitions(swim_metal_renderer PRIVATE
    SWIM_METAL_SHADER_SOURCE_PATH="${CMAKE_CURRENT_SOURCE_DIR}/cpp/backends/metal/shaders/stitch.metal")
  target_compile_options(swim_metal_renderer PRIVATE -fobjc-arc)
  target_link_libraries(swim_metal_renderer PUBLIC
    swim_core
    ${SWIM_METAL_FRAMEWORK}
    ${SWIM_COREVIDEO_FRAMEWORK}
    ${SWIM_COREMEDIA_FRAMEWORK}
    ${SWIM_IMAGEIO_FRAMEWORK}
    ${SWIM_FOUNDATION_FRAMEWORK}
    ${SWIM_IOSURFACE_FRAMEWORK}
    ${SWIM_COREGRAPHICS_FRAMEWORK})
  swim_enable_warnings(swim_metal_renderer)
  swim_enable_sanitizers(swim_metal_renderer)

  add_executable(metal_golden_test cpp/tests/metal_golden_test.mm)
  add_dependencies(metal_golden_test runtime_asset)
  target_compile_options(metal_golden_test PRIVATE -fobjc-arc)
  target_link_libraries(metal_golden_test PRIVATE swim_metal_renderer)
  swim_enable_warnings(metal_golden_test)
  swim_enable_sanitizers(metal_golden_test)
endif()
