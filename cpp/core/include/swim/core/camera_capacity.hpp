#pragma once

#include <cstddef>
#include <cstdint>

namespace swim::core {

// Upper bound on simultaneous camera lanes. Every hot-path container is sized to
// this at compile time so adding lanes never allocates while frames are flowing;
// the active count comes from the config/asset and is always <= this.
//
// 16 covers the underwater panorama (16 planes stitched left-to-right) and still
// admits the 6-camera pool layout. Raising it costs fixed memory in
// RuntimeCounters and RenderSnapshot only — no behavioural change.
inline constexpr std::size_t kMaxCameras = 16;

}  // namespace swim::core
