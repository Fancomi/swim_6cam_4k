#include <swim/d3d11/d3d11_preview.hpp>

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <d3dcompiler.h>

#include <array>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>

namespace swim::d3d11 {
namespace {

constexpr wchar_t kWindowClass[] = L"SwimD3D11PreviewWindow";
// Present the 5002x2102 composite scaled into a modest window.
constexpr UINT kWindowWidth = 1251;
constexpr UINT kWindowHeight = 526;

// Minimal blit: fullscreen triangle from SV_VertexID sampling the composite
// SRV. No vertex buffer needed.
constexpr char kBlitShader[] = R"hlsl(
Texture2D<float4> source : register(t0);
SamplerState linear_clamp : register(s0);

struct VSOut {
  float4 position : SV_Position;
  float2 uv : TEXCOORD0;
};

VSOut blit_vertex(uint id : SV_VertexID) {
  VSOut output;
  output.uv = float2((id << 1) & 2, id & 2);
  output.position = float4(output.uv * float2(2, -2) + float2(-1, 1), 0, 1);
  return output;
}

float4 blit_pixel(VSOut input) : SV_Target {
  return float4(source.Sample(linear_clamp, input.uv).rgb, 1.0f);
}
)hlsl";

std::string hr_error(const char* operation, HRESULT hr) {
  char buffer[32];
  std::snprintf(buffer, sizeof(buffer), "0x%08lx",
                static_cast<unsigned long>(hr));
  return std::string(operation) + " failed (HRESULT " + buffer + ")";
}

}  // namespace

class D3D11Preview::Impl {
 public:
  Impl(std::shared_ptr<D3D11Context> context, std::uint32_t width,
       std::uint32_t height, swim::core::RuntimeCounters& metrics,
       CloseCallback close_callback, bool visible)
      : context_(std::move(context)),
        width_(width),
        height_(height),
        metrics_(metrics),
        close_callback_(std::move(close_callback)),
        visible_(visible) {
    if (context_ == nullptr || context_->device == nullptr) {
      throw std::invalid_argument("D3D11 preview requires a valid context");
    }
    build_blit_pipeline();
  }

  ~Impl() { destroy_window(); }

  bool offer(D3D11OutputLease output) noexcept {
    std::lock_guard lock(mutex_);
    pending_ = std::move(output);
    has_pending_ = true;
    return true;
  }

  void run_main_loop(std::stop_token token) {
    if (visible_) {
      create_window();
    }
    while (!token.stop_requested() &&
           !stop_requested_.load(std::memory_order_acquire)) {
      if (visible_) {
        MSG message;
        while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
          if (message.message == WM_QUIT) {
            request_stop();
            break;
          }
          TranslateMessage(&message);
          DispatchMessageW(&message);
        }
      }
      present_latest();
      // ~120 Hz poll keeps the window responsive without busy-spinning.
      std::this_thread::sleep_for(std::chrono::milliseconds{8});
    }
    destroy_window();
  }

  void request_stop() noexcept {
    stop_requested_.store(true, std::memory_order_release);
  }

 private:
  void build_blit_pipeline() {
    ComPtr<ID3DBlob> vs;
    ComPtr<ID3DBlob> ps;
    ComPtr<ID3DBlob> errors;
    if (FAILED(D3DCompile(kBlitShader, sizeof(kBlitShader) - 1, "blit.hlsl",
                          nullptr, nullptr, "blit_vertex", "vs_5_0", 0, 0,
                          vs.GetAddressOf(), errors.GetAddressOf()))) {
      throw std::runtime_error("cannot compile preview vertex shader");
    }
    if (FAILED(D3DCompile(kBlitShader, sizeof(kBlitShader) - 1, "blit.hlsl",
                          nullptr, nullptr, "blit_pixel", "ps_5_0", 0, 0,
                          ps.GetAddressOf(), errors.GetAddressOf()))) {
      throw std::runtime_error("cannot compile preview pixel shader");
    }
    auto* device = context_->device.Get();
    if (FAILED(device->CreateVertexShader(vs->GetBufferPointer(),
                                          vs->GetBufferSize(), nullptr,
                                          vertex_shader_.GetAddressOf())) ||
        FAILED(device->CreatePixelShader(ps->GetBufferPointer(),
                                         ps->GetBufferSize(), nullptr,
                                         pixel_shader_.GetAddressOf()))) {
      throw std::runtime_error("cannot create preview shaders");
    }
    D3D11_SAMPLER_DESC sampler{};
    sampler.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sampler.AddressU = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.AddressV = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.MaxLOD = D3D11_FLOAT32_MAX;
    if (FAILED(device->CreateSamplerState(&sampler, sampler_.GetAddressOf()))) {
      throw std::runtime_error("cannot create preview sampler");
    }
  }

  static LRESULT CALLBACK window_proc(HWND hwnd, UINT message, WPARAM wparam,
                                      LPARAM lparam) {
    if (message == WM_CLOSE || message == WM_DESTROY) {
      auto* self = reinterpret_cast<Impl*>(
          GetWindowLongPtrW(hwnd, GWLP_USERDATA));
      if (self != nullptr && self->close_callback_) {
        self->close_callback_();
      }
      if (message == WM_DESTROY) {
        PostQuitMessage(0);
      }
      return 0;
    }
    return DefWindowProcW(hwnd, message, wparam, lparam);
  }

  void create_window() {
    WNDCLASSEXW wc{};
    wc.cbSize = sizeof(wc);
    wc.lpfnWndProc = &Impl::window_proc;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = kWindowClass;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    RegisterClassExW(&wc);

    RECT rect{0, 0, static_cast<LONG>(kWindowWidth),
              static_cast<LONG>(kWindowHeight)};
    AdjustWindowRect(&rect, WS_OVERLAPPEDWINDOW, FALSE);
    hwnd_ = CreateWindowExW(0, kWindowClass, L"swim realtime preview",
                            WS_OVERLAPPEDWINDOW, CW_USEDEFAULT, CW_USEDEFAULT,
                            rect.right - rect.left, rect.bottom - rect.top,
                            nullptr, nullptr, wc.hInstance, nullptr);
    if (hwnd_ == nullptr) {
      throw std::runtime_error("cannot create preview window");
    }
    SetWindowLongPtrW(hwnd_, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(this));

    DXGI_SWAP_CHAIN_DESC1 desc{};
    desc.Width = kWindowWidth;
    desc.Height = kWindowHeight;
    desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    desc.BufferCount = 2;
    desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    if (FAILED(context_->factory->CreateSwapChainForHwnd(
            context_->device.Get(), hwnd_, &desc, nullptr, nullptr,
            swap_chain_.GetAddressOf()))) {
      throw std::runtime_error("cannot create preview swap chain");
    }
    create_backbuffer_rtv();
    ShowWindow(hwnd_, SW_SHOW);
    UpdateWindow(hwnd_);
  }

  void create_backbuffer_rtv() {
    ComPtr<ID3D11Texture2D> backbuffer;
    if (FAILED(swap_chain_->GetBuffer(
            0, __uuidof(ID3D11Texture2D),
            reinterpret_cast<void**>(backbuffer.GetAddressOf())))) {
      throw std::runtime_error("cannot obtain preview backbuffer");
    }
    if (FAILED(context_->device->CreateRenderTargetView(
            backbuffer.Get(), nullptr, backbuffer_rtv_.ReleaseAndGetAddressOf()))) {
      throw std::runtime_error("cannot create preview backbuffer view");
    }
  }

  void present_latest() {
    D3D11OutputLease latest;
    {
      std::lock_guard lock(mutex_);
      if (!has_pending_) {
        return;
      }
      latest = std::move(pending_);
      has_pending_ = false;
    }
    if (!latest) {
      return;
    }
    if (!visible_ || swap_chain_ == nullptr) {
      // Offscreen preview: count the present but do no window work.
      metrics_.preview_presents.fetch_add(1, std::memory_order_relaxed);
      return;
    }

    std::lock_guard lock(context_->context_mutex);
    auto* ctx = context_->immediate_context.Get();
    ID3D11RenderTargetView* rtv = backbuffer_rtv_.Get();
    ctx->OMSetRenderTargets(1, &rtv, nullptr);
    D3D11_VIEWPORT viewport{};
    viewport.Width = static_cast<float>(kWindowWidth);
    viewport.Height = static_cast<float>(kWindowHeight);
    viewport.MaxDepth = 1.0F;
    ctx->RSSetViewports(1, &viewport);
    ctx->IASetInputLayout(nullptr);
    ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(vertex_shader_.Get(), nullptr, 0);
    ctx->PSSetShader(pixel_shader_.Get(), nullptr, 0);
    ID3D11ShaderResourceView* srv = latest.srv();
    ctx->PSSetShaderResources(0, 1, &srv);
    ID3D11SamplerState* sampler = sampler_.Get();
    ctx->PSSetSamplers(0, 1, &sampler);
    ctx->Draw(3, 0);
    ID3D11ShaderResourceView* null_srv = nullptr;
    ctx->PSSetShaderResources(0, 1, &null_srv);
    swap_chain_->Present(1, 0);
    metrics_.preview_presents.fetch_add(1, std::memory_order_relaxed);
  }

  void destroy_window() {
    backbuffer_rtv_.Reset();
    swap_chain_.Reset();
    if (hwnd_ != nullptr) {
      DestroyWindow(hwnd_);
      hwnd_ = nullptr;
    }
  }

  std::shared_ptr<D3D11Context> context_;
  std::uint32_t width_;
  std::uint32_t height_;
  swim::core::RuntimeCounters& metrics_;
  CloseCallback close_callback_;
  bool visible_;
  HWND hwnd_ = nullptr;
  ComPtr<IDXGISwapChain1> swap_chain_;
  ComPtr<ID3D11RenderTargetView> backbuffer_rtv_;
  ComPtr<ID3D11VertexShader> vertex_shader_;
  ComPtr<ID3D11PixelShader> pixel_shader_;
  ComPtr<ID3D11SamplerState> sampler_;
  std::mutex mutex_;
  D3D11OutputLease pending_;
  bool has_pending_ = false;
  std::atomic_bool stop_requested_{false};
};

D3D11Preview::D3D11Preview(std::shared_ptr<D3D11Context> context,
                           std::uint32_t width, std::uint32_t height,
                           swim::core::RuntimeCounters& metrics,
                           CloseCallback close_callback, bool visible)
    : impl_(std::make_shared<Impl>(std::move(context), width, height, metrics,
                                   std::move(close_callback), visible)) {}

D3D11Preview::~D3D11Preview() = default;

bool D3D11Preview::offer(D3D11OutputLease output) noexcept {
  return impl_->offer(std::move(output));
}

void D3D11Preview::run_main_loop(std::stop_token token) {
  impl_->run_main_loop(token);
}

void D3D11Preview::request_stop() noexcept { impl_->request_stop(); }

}  // namespace swim::d3d11

