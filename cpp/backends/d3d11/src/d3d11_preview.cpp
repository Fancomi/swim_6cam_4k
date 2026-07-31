#include <swim/d3d11/d3d11_preview.hpp>

#include <swim/core/preview_layout.hpp>

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <d3dcompiler.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <utility>

namespace swim::d3d11 {
namespace {

constexpr wchar_t kWindowClass[] = L"SwimD3D11PreviewWindow";

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
    if (width_ == 0 || height_ == 0) {
      throw std::invalid_argument("D3D11 preview requires nonzero dimensions");
    }
    // Open at the composite's own aspect ratio. The three lines range from
    // 2.4:1 to 9.1:1, so one baked-in window size fits at most one of them.
    std::tie(window_width_, window_height_) =
        swim::core::preview_target_size(width_, height_);
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
    auto* self =
        reinterpret_cast<Impl*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    if (message == WM_CLOSE || message == WM_DESTROY) {
      if (self != nullptr && self->close_callback_) {
        self->close_callback_();
      }
      if (message == WM_DESTROY) {
        PostQuitMessage(0);
      }
      return 0;
    }
    if (message == WM_SIZING && self != nullptr) {
      // Constrain the live drag to the composite's ratio, so the window the
      // user lets go of is already a scaled view of the whole stitch.
      self->constrain_drag(static_cast<UINT>(wparam),
                           reinterpret_cast<RECT*>(lparam));
      return TRUE;
    }
    if (message == WM_SIZE && self != nullptr) {
      // Deferred: the swap chain has to be resized with the device context
      // mutex held, and present_latest() is where that lock is taken.
      self->resize_pending_.store(true, std::memory_order_release);
      return 0;
    }
    return DefWindowProcW(hwnd, message, wparam, lparam);
  }

  // Snap a WM_SIZING drag rectangle to the composite's aspect ratio. The ratio
  // applies to the client area, so the frame is subtracted first and added back
  // afterwards; which edge the user grabbed decides whether width follows
  // height or the other way round.
  void constrain_drag(UINT edge, RECT* rect) const noexcept {
    if (rect == nullptr) {
      return;
    }
    RECT frame{0, 0, 0, 0};
    AdjustWindowRect(&frame, WS_OVERLAPPEDWINDOW, FALSE);
    const LONG frame_width = (frame.right - frame.left);
    const LONG frame_height = (frame.bottom - frame.top);
    const LONG client_width = (rect->right - rect->left) - frame_width;
    const LONG client_height = (rect->bottom - rect->top) - frame_height;
    if (client_width <= 0 || client_height <= 0) {
      return;
    }
    const double aspect =
        static_cast<double>(width_) / static_cast<double>(height_);

    // Dragging a vertical edge fixes the width and derives the height; a
    // horizontal edge does the reverse. Corners follow the width, which is the
    // dominant axis for these panoramas.
    LONG target_width = client_width;
    LONG target_height = client_height;
    switch (edge) {
      case WMSZ_TOP:
      case WMSZ_BOTTOM:
        target_width = std::max<LONG>(
            1, static_cast<LONG>(std::lround(client_height * aspect)));
        break;
      default:
        target_height = std::max<LONG>(
            1, static_cast<LONG>(std::lround(client_width / aspect)));
        break;
    }

    // Grow away from the edge being held so the grabbed side stays under the
    // cursor instead of sliding out from under it.
    const LONG width_delta = (target_width + frame_width) -
                             (rect->right - rect->left);
    const LONG height_delta = (target_height + frame_height) -
                              (rect->bottom - rect->top);
    switch (edge) {
      case WMSZ_LEFT:
      case WMSZ_TOPLEFT:
      case WMSZ_BOTTOMLEFT:
        rect->left -= width_delta;
        break;
      default:
        rect->right += width_delta;
        break;
    }
    switch (edge) {
      case WMSZ_TOP:
      case WMSZ_TOPLEFT:
      case WMSZ_TOPRIGHT:
        rect->top -= height_delta;
        break;
      default:
        rect->bottom += height_delta;
        break;
    }
  }

  void create_window() {
    WNDCLASSEXW wc{};
    wc.cbSize = sizeof(wc);
    wc.lpfnWndProc = &Impl::window_proc;
    wc.hInstance = GetModuleHandleW(nullptr);
    wc.lpszClassName = kWindowClass;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    RegisterClassExW(&wc);

    RECT rect{0, 0, static_cast<LONG>(window_width_),
              static_cast<LONG>(window_height_)};
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
    desc.Width = window_width_;
    desc.Height = window_height_;
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

  // Match the swap chain to the client area. Called with the device context
  // mutex held; the RTV must be released before ResizeBuffers or DXGI refuses
  // with DXGI_ERROR_INVALID_CALL.
  void resize_swap_chain() noexcept {
    RECT client{};
    if (GetClientRect(hwnd_, &client) == 0) {
      return;
    }
    const auto width = static_cast<UINT>(std::max<LONG>(1, client.right));
    const auto height = static_cast<UINT>(std::max<LONG>(1, client.bottom));
    if (width == window_width_ && height == window_height_) {
      return;
    }
    ID3D11RenderTargetView* none = nullptr;
    context_->immediate_context->OMSetRenderTargets(1, &none, nullptr);
    backbuffer_rtv_.Reset();
    if (FAILED(swap_chain_->ResizeBuffers(0, width, height,
                                         DXGI_FORMAT_UNKNOWN, 0))) {
      // Keep presenting at the old size rather than tearing down the run; the
      // next resize gets another chance.
      create_backbuffer_rtv_noexcept();
      return;
    }
    window_width_ = width;
    window_height_ = height;
    create_backbuffer_rtv_noexcept();
  }

  void create_backbuffer_rtv_noexcept() noexcept {
    try {
      create_backbuffer_rtv();
    } catch (...) {
      backbuffer_rtv_.Reset();
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
    if (resize_pending_.exchange(false, std::memory_order_acq_rel)) {
      resize_swap_chain();
    }
    if (backbuffer_rtv_ == nullptr) {
      return;
    }
    auto* ctx = context_->immediate_context.Get();
    ID3D11RenderTargetView* rtv = backbuffer_rtv_.Get();
    ctx->OMSetRenderTargets(1, &rtv, nullptr);
    // Clear first: a window dragged off-ratio, maximised, or snapped shows the
    // content letterboxed inside these bars instead of stretched to fit.
    constexpr std::array<float, 4> black{0.0F, 0.0F, 0.0F, 1.0F};
    ctx->ClearRenderTargetView(rtv, black.data());
    const auto box = swim::core::preview_viewport(window_width_, window_height_,
                                                 width_, height_);
    D3D11_VIEWPORT viewport{};
    viewport.TopLeftX = static_cast<float>(box.x);
    viewport.TopLeftY = static_cast<float>(box.y);
    viewport.Width = static_cast<float>(box.width);
    viewport.Height = static_cast<float>(box.height);
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
  // Client-area size of the preview window, tracking the composite's aspect at
  // creation and the user's drags afterwards.
  UINT window_width_{};
  UINT window_height_{};
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
  std::atomic_bool resize_pending_{false};
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

