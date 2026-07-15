#include <swim/cudagl/gl_loader.hpp>

#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include <stdexcept>
#include <string>

namespace swim::cudagl {

// Definitions of the pointer table declared in the header.
void (*glGenTextures)(GLsizei, GLuint*) = nullptr;
void (*glBindTexture)(GLenum, GLuint) = nullptr;
void (*glDeleteTextures)(GLsizei, const GLuint*) = nullptr;
void (*glTexParameteri)(GLenum, GLenum, GLint) = nullptr;
void (*glTexImage2D)(GLenum, GLint, GLint, GLsizei, GLsizei, GLint, GLenum,
                     GLenum, const void*) = nullptr;
void (*glPixelStorei)(GLenum, GLint) = nullptr;
void (*glTexSubImage2D)(GLenum, GLint, GLint, GLint, GLsizei, GLsizei, GLenum,
                        GLenum, const void*) = nullptr;
void (*glActiveTexture)(GLenum) = nullptr;
void (*glGenBuffers)(GLsizei, GLuint*) = nullptr;
void (*glBindBuffer)(GLenum, GLuint) = nullptr;
void (*glBufferData)(GLenum, GLsizeiptr, const void*, GLenum) = nullptr;
void (*glDeleteBuffers)(GLsizei, const GLuint*) = nullptr;
void (*glGenVertexArrays)(GLsizei, GLuint*) = nullptr;
void (*glBindVertexArray)(GLuint) = nullptr;
void (*glDeleteVertexArrays)(GLsizei, const GLuint*) = nullptr;
void (*glEnableVertexAttribArray)(GLuint) = nullptr;
void (*glVertexAttribPointer)(GLuint, GLint, GLenum, GLboolean, GLsizei,
                              const void*) = nullptr;
GLuint (*glCreateShader)(GLenum) = nullptr;
void (*glShaderSource)(GLuint, GLsizei, const GLchar* const*,
                       const GLint*) = nullptr;
void (*glCompileShader)(GLuint) = nullptr;
void (*glGetShaderiv)(GLuint, GLenum, GLint*) = nullptr;
void (*glGetShaderInfoLog)(GLuint, GLsizei, GLsizei*, GLchar*) = nullptr;
void (*glDeleteShader)(GLuint) = nullptr;
GLuint (*glCreateProgram)() = nullptr;
void (*glAttachShader)(GLuint, GLuint) = nullptr;
void (*glLinkProgram)(GLuint) = nullptr;
void (*glGetProgramiv)(GLuint, GLenum, GLint*) = nullptr;
void (*glGetProgramInfoLog)(GLuint, GLsizei, GLsizei*, GLchar*) = nullptr;
void (*glUseProgram)(GLuint) = nullptr;
void (*glDeleteProgram)(GLuint) = nullptr;
GLint (*glGetUniformLocation)(GLuint, const GLchar*) = nullptr;
void (*glUniform1i)(GLint, GLint) = nullptr;
void (*glUniform1f)(GLint, GLfloat) = nullptr;
void (*glUniform2f)(GLint, GLfloat, GLfloat) = nullptr;
void (*glUniform1ui)(GLint, GLuint) = nullptr;
void (*glUniform4i)(GLint, GLint, GLint, GLint, GLint) = nullptr;
void (*glGenFramebuffers)(GLsizei, GLuint*) = nullptr;
void (*glBindFramebuffer)(GLenum, GLuint) = nullptr;
void (*glFramebufferTexture2D)(GLenum, GLenum, GLenum, GLuint, GLint) = nullptr;
GLenum (*glCheckFramebufferStatus)(GLenum) = nullptr;
void (*glDeleteFramebuffers)(GLsizei, const GLuint*) = nullptr;
void (*glDrawElements)(GLenum, GLsizei, GLenum, const void*) = nullptr;
void (*glDrawArrays)(GLenum, GLint, GLsizei) = nullptr;
void (*glViewport)(GLint, GLint, GLsizei, GLsizei) = nullptr;
void (*glClear)(GLbitfield) = nullptr;
void (*glClearColor)(GLfloat, GLfloat, GLfloat, GLfloat) = nullptr;
void (*glEnable)(GLenum) = nullptr;
void (*glDisable)(GLenum) = nullptr;
void (*glBlendFunc)(GLenum, GLenum) = nullptr;
void (*glBlendEquation)(GLenum) = nullptr;
void (*glFinish)() = nullptr;
void (*glReadPixels)(GLint, GLint, GLsizei, GLsizei, GLenum, GLenum,
                     void*) = nullptr;
GLenum (*glGetError)() = nullptr;

namespace {

template <typename Fn>
void load_one(Fn& target, const char* name) {
  target = reinterpret_cast<Fn>(glfwGetProcAddress(name));
  if (target == nullptr) {
    throw std::runtime_error(std::string("missing GL entry point: ") + name);
  }
}

}  // namespace

void load_gl_functions() {
  load_one(glGenTextures, "glGenTextures");
  load_one(glBindTexture, "glBindTexture");
  load_one(glDeleteTextures, "glDeleteTextures");
  load_one(glTexParameteri, "glTexParameteri");
  load_one(glTexImage2D, "glTexImage2D");
  load_one(glPixelStorei, "glPixelStorei");
  load_one(glTexSubImage2D, "glTexSubImage2D");
  load_one(glActiveTexture, "glActiveTexture");
  load_one(glGenBuffers, "glGenBuffers");
  load_one(glBindBuffer, "glBindBuffer");
  load_one(glBufferData, "glBufferData");
  load_one(glDeleteBuffers, "glDeleteBuffers");
  load_one(glGenVertexArrays, "glGenVertexArrays");
  load_one(glBindVertexArray, "glBindVertexArray");
  load_one(glDeleteVertexArrays, "glDeleteVertexArrays");
  load_one(glEnableVertexAttribArray, "glEnableVertexAttribArray");
  load_one(glVertexAttribPointer, "glVertexAttribPointer");
  load_one(glCreateShader, "glCreateShader");
  load_one(glShaderSource, "glShaderSource");
  load_one(glCompileShader, "glCompileShader");
  load_one(glGetShaderiv, "glGetShaderiv");
  load_one(glGetShaderInfoLog, "glGetShaderInfoLog");
  load_one(glDeleteShader, "glDeleteShader");
  load_one(glCreateProgram, "glCreateProgram");
  load_one(glAttachShader, "glAttachShader");
  load_one(glLinkProgram, "glLinkProgram");
  load_one(glGetProgramiv, "glGetProgramiv");
  load_one(glGetProgramInfoLog, "glGetProgramInfoLog");
  load_one(glUseProgram, "glUseProgram");
  load_one(glDeleteProgram, "glDeleteProgram");
  load_one(glGetUniformLocation, "glGetUniformLocation");
  load_one(glUniform1i, "glUniform1i");
  load_one(glUniform1f, "glUniform1f");
  load_one(glUniform2f, "glUniform2f");
  load_one(glUniform1ui, "glUniform1ui");
  load_one(glUniform4i, "glUniform4i");
  load_one(glGenFramebuffers, "glGenFramebuffers");
  load_one(glBindFramebuffer, "glBindFramebuffer");
  load_one(glFramebufferTexture2D, "glFramebufferTexture2D");
  load_one(glCheckFramebufferStatus, "glCheckFramebufferStatus");
  load_one(glDeleteFramebuffers, "glDeleteFramebuffers");
  load_one(glDrawElements, "glDrawElements");
  load_one(glDrawArrays, "glDrawArrays");
  load_one(glViewport, "glViewport");
  load_one(glClear, "glClear");
  load_one(glClearColor, "glClearColor");
  load_one(glEnable, "glEnable");
  load_one(glDisable, "glDisable");
  load_one(glBlendFunc, "glBlendFunc");
  load_one(glBlendEquation, "glBlendEquation");
  load_one(glFinish, "glFinish");
  load_one(glReadPixels, "glReadPixels");
  load_one(glGetError, "glGetError");
}

}  // namespace swim::cudagl
