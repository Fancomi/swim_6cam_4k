#pragma once

namespace swim::cudagl {

// Registers the "cudagl" backend factory with the core BackendRegistry.
// Safe to call more than once; only the first call registers.
void register_cudagl_backend();

}  // namespace swim::cudagl
