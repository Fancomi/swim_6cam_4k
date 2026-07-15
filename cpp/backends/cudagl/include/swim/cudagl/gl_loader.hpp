#pragma once

// Minimal OpenGL 3.3 core loader. opengl32.dll on Windows only exports GL 1.1,
// so every modern entry point is resolved through glfwGetProcAddress at runtime.
// We avoid GLEW entirely and declare exactly the subset this backend uses.
//
// GLFW_INCLUDE_NONE keeps <GL/gl.h> out so our own typedefs/enums are the single
// source of truth.

#include <cstddef>
#include <cstdint>

namespace swim::cudagl {

using GLenum = unsigned int;
using GLbitfield = unsigned int;
using GLuint = unsigned int;
using GLint = int;
using GLsizei = int;
using GLfloat = float;
using GLboolean = unsigned char;
using GLchar = char;
using GLintptr = std::intptr_t;
using GLsizeiptr = std::intptr_t;
using GLvoid = void;

// Enums (values from the GL spec; stable ABI constants).
inline constexpr GLenum GL_FALSE = 0;
inline constexpr GLenum GL_TRUE = 1;
inline constexpr GLenum GL_TRIANGLES = 0x0004;
inline constexpr GLenum GL_UNSIGNED_INT = 0x1405;
inline constexpr GLenum GL_FLOAT = 0x1406;
inline constexpr GLenum GL_UNSIGNED_BYTE = 0x1401;
inline constexpr GLenum GL_UNSIGNED_SHORT = 0x1403;
inline constexpr GLenum GL_COLOR_BUFFER_BIT = 0x00004000;
inline constexpr GLenum GL_TEXTURE_2D = 0x0DE1;
inline constexpr GLenum GL_TEXTURE0 = 0x84C0;
inline constexpr GLenum GL_TEXTURE_MIN_FILTER = 0x2801;
inline constexpr GLenum GL_TEXTURE_MAG_FILTER = 0x2800;
inline constexpr GLenum GL_TEXTURE_WRAP_S = 0x2802;
inline constexpr GLenum GL_TEXTURE_WRAP_T = 0x2803;
inline constexpr GLenum GL_LINEAR = 0x2601;
inline constexpr GLenum GL_CLAMP_TO_EDGE = 0x812F;
inline constexpr GLenum GL_MIRRORED_REPEAT = 0x8370;
inline constexpr GLenum GL_RED = 0x1903;
inline constexpr GLenum GL_RG = 0x8227;
inline constexpr GLenum GL_RGBA = 0x1908;
inline constexpr GLenum GL_BGRA = 0x80E1;
inline constexpr GLenum GL_R8 = 0x8229;
inline constexpr GLenum GL_RG8 = 0x822B;
inline constexpr GLenum GL_R16 = 0x822A;
inline constexpr GLenum GL_RGBA8 = 0x8058;
inline constexpr GLenum GL_RGBA16F = 0x881A;
inline constexpr GLenum GL_ARRAY_BUFFER = 0x8892;
inline constexpr GLenum GL_ELEMENT_ARRAY_BUFFER = 0x8893;
inline constexpr GLenum GL_STATIC_DRAW = 0x88E4;
inline constexpr GLenum GL_DYNAMIC_DRAW = 0x88E8;
inline constexpr GLenum GL_FRAGMENT_SHADER = 0x8B30;
inline constexpr GLenum GL_VERTEX_SHADER = 0x8B31;
inline constexpr GLenum GL_COMPILE_STATUS = 0x8B81;
inline constexpr GLenum GL_LINK_STATUS = 0x8B82;
inline constexpr GLenum GL_INFO_LOG_LENGTH = 0x8B84;
inline constexpr GLenum GL_FRAMEBUFFER = 0x8D40;
inline constexpr GLenum GL_COLOR_ATTACHMENT0 = 0x8CE0;
inline constexpr GLenum GL_FRAMEBUFFER_COMPLETE = 0x8CD5;
inline constexpr GLenum GL_BLEND = 0x0BE2;
inline constexpr GLenum GL_ONE = 1;
inline constexpr GLenum GL_FUNC_ADD = 0x8006;
inline constexpr GLenum GL_CULL_FACE = 0x0B44;
inline constexpr GLenum GL_NO_ERROR = 0;
inline constexpr GLenum GL_UNPACK_ALIGNMENT = 0x0CF5;

// Loads all entry points through glfwGetProcAddress. Must be called once with a
// current GL context. Throws std::runtime_error if a required function or the
// minimum GL version is missing.
void load_gl_functions();

// Function-pointer table. Populated by load_gl_functions(); call through these.
// Named gl* so call sites read like ordinary GL.
extern void (*glGenTextures)(GLsizei, GLuint*);
extern void (*glBindTexture)(GLenum, GLuint);
extern void (*glDeleteTextures)(GLsizei, const GLuint*);
extern void (*glTexParameteri)(GLenum, GLenum, GLint);
extern void (*glTexImage2D)(GLenum, GLint, GLint, GLsizei, GLsizei, GLint,
                            GLenum, GLenum, const void*);
extern void (*glPixelStorei)(GLenum, GLint);
extern void (*glTexSubImage2D)(GLenum, GLint, GLint, GLint, GLsizei, GLsizei,
                               GLenum, GLenum, const void*);
extern void (*glActiveTexture)(GLenum);
extern void (*glGenBuffers)(GLsizei, GLuint*);
extern void (*glBindBuffer)(GLenum, GLuint);
extern void (*glBufferData)(GLenum, GLsizeiptr, const void*, GLenum);
extern void (*glDeleteBuffers)(GLsizei, const GLuint*);
extern void (*glGenVertexArrays)(GLsizei, GLuint*);
extern void (*glBindVertexArray)(GLuint);
extern void (*glDeleteVertexArrays)(GLsizei, const GLuint*);
extern void (*glEnableVertexAttribArray)(GLuint);
extern void (*glVertexAttribPointer)(GLuint, GLint, GLenum, GLboolean, GLsizei,
                                     const void*);
extern GLuint (*glCreateShader)(GLenum);
extern void (*glShaderSource)(GLuint, GLsizei, const GLchar* const*,
                              const GLint*);
extern void (*glCompileShader)(GLuint);
extern void (*glGetShaderiv)(GLuint, GLenum, GLint*);
extern void (*glGetShaderInfoLog)(GLuint, GLsizei, GLsizei*, GLchar*);
extern void (*glDeleteShader)(GLuint);
extern GLuint (*glCreateProgram)();
extern void (*glAttachShader)(GLuint, GLuint);
extern void (*glLinkProgram)(GLuint);
extern void (*glGetProgramiv)(GLuint, GLenum, GLint*);
extern void (*glGetProgramInfoLog)(GLuint, GLsizei, GLsizei*, GLchar*);
extern void (*glUseProgram)(GLuint);
extern void (*glDeleteProgram)(GLuint);
extern GLint (*glGetUniformLocation)(GLuint, const GLchar*);
extern void (*glUniform1i)(GLint, GLint);
extern void (*glUniform1f)(GLint, GLfloat);
extern void (*glUniform2f)(GLint, GLfloat, GLfloat);
extern void (*glUniform1ui)(GLint, GLuint);
extern void (*glUniform4i)(GLint, GLint, GLint, GLint, GLint);
extern void (*glGenFramebuffers)(GLsizei, GLuint*);
extern void (*glBindFramebuffer)(GLenum, GLuint);
extern void (*glFramebufferTexture2D)(GLenum, GLenum, GLenum, GLuint, GLint);
extern GLenum (*glCheckFramebufferStatus)(GLenum);
extern void (*glDeleteFramebuffers)(GLsizei, const GLuint*);
extern void (*glDrawElements)(GLenum, GLsizei, GLenum, const void*);
extern void (*glDrawArrays)(GLenum, GLint, GLsizei);
extern void (*glViewport)(GLint, GLint, GLsizei, GLsizei);
extern void (*glClear)(GLbitfield);
extern void (*glClearColor)(GLfloat, GLfloat, GLfloat, GLfloat);
extern void (*glEnable)(GLenum);
extern void (*glDisable)(GLenum);
extern void (*glBlendFunc)(GLenum, GLenum);
extern void (*glBlendEquation)(GLenum);
extern void (*glFinish)();
extern void (*glReadPixels)(GLint, GLint, GLsizei, GLsizei, GLenum, GLenum,
                            void*);
extern GLenum (*glGetError)();

}  // namespace swim::cudagl

