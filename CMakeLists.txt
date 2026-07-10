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
  cpp/core/src/camera_health.cpp
  cpp/core/src/frame.cpp
  cpp/core/src/hot_path_allocations.cpp
  cpp/core/src/metrics.cpp)
target_include_directories(swim_core PUBLIC cpp/core/include)
swim_enable_warnings(swim_core)
swim_enable_sanitizers(swim_core)

enable_testing()
add_executable(swim_core_tests
  cpp/tests/test_main.cpp
  cpp/tests/test_asset.cpp
  cpp/tests/test_camera_health.cpp
  cpp/tests/test_frame_mailbox.cpp
  cpp/tests/test_metrics.cpp)
target_link_libraries(swim_core_tests PRIVATE swim_core)
target_compile_definitions(swim_core_tests PRIVATE
  SWIM_TEST_ASSET_PATH="${RUNTIME_ASSET}")
swim_enable_warnings(swim_core_tests)
swim_enable_sanitizers(swim_core_tests)
add_dependencies(swim_core_tests runtime_asset)
add_test(NAME swim_core_tests COMMAND swim_core_tests)
