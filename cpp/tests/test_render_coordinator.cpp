#include "test_support.hpp"

#include <swim/core/render_coordinator.hpp>

TEST_CASE(render_coordinator_contract_is_available) {
  CHECK(swim::core::RenderCoordinator::kCameraCount == 6);
}
