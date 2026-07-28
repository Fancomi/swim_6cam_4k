#pragma once

namespace swim::d3d11 {

// Registers the "d3d11" backend factory with the core BackendRegistry. Safe to
// call more than once; only the first call registers.
void register_d3d11_backend();

}  // namespace swim::d3d11
