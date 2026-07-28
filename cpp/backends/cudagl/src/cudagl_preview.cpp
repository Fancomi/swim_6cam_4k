#include <swim/cudagl/cudagl_preview.hpp>

#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include <atomic>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>

namespace swim::cudagl {
namespace {

constexpr int kWindowWidth = 1251;
constexpr int kWindowHeight = 526;

// Blit the composite RGBA texture to the window with a fullscreen triangle.
const char* kBlitVertex = R"GLSL(#version 330 core
out vec2 v_uv;
void main(){
  vec2 p = vec2((gl_VertexID<<1)&2, gl_VertexID&2);
  v_uv = vec2(p.x, 1.0 - p.y);
  gl_Position = vec4(p*2.0-1.0, 0.0, 1.0);
}
)GLSL";

const char* kBlitFragment = R"GLSL(#version 330 core
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_tex;
void main(){ frag = vec4(texture(u_tex, v_uv).rgb, 1.0); }
)GLSL";

GLuint compile(GLenum type, const char* src) {
  GLuint s = glCreateShader(type);
  glShaderSource(s, 1, &src, nullptr);
  glCompileShader(s);
  GLint ok = 0;
  glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
  if (ok == 0) {
    throw std::runtime_error("preview shader compile failed");
  }
  return s;
}

}  // namespace

class CudaGlPreview::Impl {
 public:
  Impl(std::shared_ptr<CudaGlContext> context, std::uint32_t width,
       std::uint32_t height, swim::core::RuntimeCounters& metrics,
       CloseCallback close_callback, bool visible)
      : context_(std::move(context)),
        width_(width),
        height_(height),
        metrics_(metrics),
        close_callback_(std::move(close_callback)),
        visible_(visible) {}

  ~Impl() { destroy_window(); }

  void offer(GLuint output_texture) noexcept {
    latest_texture_.store(output_texture, std::memory_order_release);
    has_pending_.store(true, std::memory_order_release);
  }

  void run_main_loop(std::stop_token token) {
    if (visible_) {
      create_window();
    }
    while (!token.stop_requested() &&
           !stop_requested_.load(std::memory_order_acquire)) {
      if (visible_ && window_ != nullptr) {
        glfwPollEvents();
        if (glfwWindowShouldClose(window_) != 0) {
          if (close_callback_) {
            close_callback_();
          }
          break;
        }
        present_latest();
      }
      std::this_thread::sleep_for(std::chrono::milliseconds{8});
    }
    destroy_window();
  }

  void request_stop() noexcept {
    stop_requested_.store(true, std::memory_order_release);
  }

 private:
  void create_window() {
    // Share the backend's GL context so the composite output texture is visible
    // here. The backend context stays current on the render thread; this window
    // context is current only on this (main) thread.
    glfwWindowHint(GLFW_CONTEXT_VERSION_MAJOR, 3);
    glfwWindowHint(GLFW_CONTEXT_VERSION_MINOR, 3);
    glfwWindowHint(GLFW_OPENGL_PROFILE, GLFW_OPENGL_CORE_PROFILE);
    glfwWindowHint(GLFW_VISIBLE, GLFW_TRUE);
    window_ = glfwCreateWindow(kWindowWidth, kWindowHeight,
                               "swim realtime preview (CUDA/GL)", nullptr,
                               context_->gl_context);
    if (window_ == nullptr) {
      throw std::runtime_error("cannot create CUDA/GL preview window");
    }
    glfwMakeContextCurrent(window_);
    glfwSwapInterval(1);
    // The GL entry-point table may not have been populated yet (the renderer
    // loads it on its own thread). glfwGetProcAddress works against any current
    // context, so load here too; it is idempotent.
    load_gl_functions();
    GLuint vs = compile(GL_VERTEX_SHADER, kBlitVertex);
    GLuint fs = compile(GL_FRAGMENT_SHADER, kBlitFragment);
    blit_program_ = glCreateProgram();
    glAttachShader(blit_program_, vs);
    glAttachShader(blit_program_, fs);
    glLinkProgram(blit_program_);
    glDeleteShader(vs);
    glDeleteShader(fs);
    glGenVertexArrays(1, &vao_);
  }

  void present_latest() {
    if (!has_pending_.load(std::memory_order_acquire)) {
      return;
    }
    const GLuint tex = latest_texture_.load(std::memory_order_acquire);
    if (tex == 0) {
      return;
    }
    int fbw = 0, fbh = 0;
    glfwGetFramebufferSize(window_, &fbw, &fbh);
    glViewport(0, 0, fbw, fbh);
    glClearColor(0, 0, 0, 1);
    glClear(GL_COLOR_BUFFER_BIT);
    glDisable(GL_BLEND);
    glUseProgram(blit_program_);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, tex);
    glUniform1i(glGetUniformLocation(blit_program_, "u_tex"), 0);
    glBindVertexArray(vao_);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    glfwSwapBuffers(window_);
    metrics_.preview_presents.fetch_add(1, std::memory_order_relaxed);
  }

  void destroy_window() {
    if (window_ != nullptr) {
      glfwDestroyWindow(window_);
      window_ = nullptr;
    }
  }

  std::shared_ptr<CudaGlContext> context_;
  std::uint32_t width_, height_;
  swim::core::RuntimeCounters& metrics_;
  CloseCallback close_callback_;
  bool visible_;
  GLFWwindow* window_ = nullptr;
  GLuint blit_program_ = 0;
  GLuint vao_ = 0;
  std::atomic<GLuint> latest_texture_{0};
  std::atomic_bool has_pending_{false};
  std::atomic_bool stop_requested_{false};
};

CudaGlPreview::CudaGlPreview(std::shared_ptr<CudaGlContext> context,
                             std::uint32_t width, std::uint32_t height,
                             swim::core::RuntimeCounters& metrics,
                             CloseCallback close_callback, bool visible)
    : impl_(std::make_shared<Impl>(std::move(context), width, height, metrics,
                                   std::move(close_callback), visible)) {}

CudaGlPreview::~CudaGlPreview() = default;

void CudaGlPreview::offer(GLuint output_texture) noexcept {
  impl_->offer(output_texture);
}
void CudaGlPreview::run_main_loop(std::stop_token token) {
  impl_->run_main_loop(token);
}
void CudaGlPreview::request_stop() noexcept { impl_->request_stop(); }

}  // namespace swim::cudagl
