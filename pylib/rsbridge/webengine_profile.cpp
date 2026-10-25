// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Creates a QWebEngineProfile, so that the caller can release the GIL while
// the constructor runs. The first profile takes hundreds of milliseconds to
// create, and PyQt holds the GIL while it constructs objects.
//
// This needs no Qt headers or libraries at build time: the constructor is
// looked up in the QtWebEngineCore library that PyQt has already loaded. Qt's
// binary compatibility promise keeps the constructor's symbol name and the
// object's size fixed within Qt 6.

#include <cstdint>
#include <cstdlib>

#if defined(_WIN32)
#include <windows.h>
#else
#include <dlfcn.h>
#endif
#if defined(__APPLE__)
#include <cstring>
#include <mach-o/dyld.h>
#endif

namespace {

// QObject (vtable and d_ptr) plus QWebEngineProfile's d_ptr; the rest is a
// safety margin.
constexpr size_t kProfileSize = 16 * sizeof(void *);

// QWebEngineProfile::QWebEngineProfile(QObject *parent)
using Constructor = void (*)(void *self, void *parent);

#if defined(_WIN32)

void *find_constructor(const char **error) {
  HMODULE library = GetModuleHandleW(L"Qt6WebEngineCore.dll");
  if (!library) {
    *error = "Qt6WebEngineCore.dll is not loaded";
    return nullptr;
  }
  void *ctor = reinterpret_cast<void *>(
      GetProcAddress(library, "??0QWebEngineProfile@@QEAA@PEAVQObject@@@Z"));
  if (!ctor) {
    *error = "QWebEngineProfile constructor not found";
  }
  return ctor;
}

#else

void *open_loaded_library(const char **error) {
#if defined(__APPLE__)
  for (uint32_t i = 0; i < _dyld_image_count(); i++) {
    const char *name = _dyld_get_image_name(i);
    const char *suffix = "/QtWebEngineCore";
    size_t len = strlen(name), suffix_len = strlen(suffix);
    if (len >= suffix_len && strcmp(name + len - suffix_len, suffix) == 0) {
      return dlopen(name, RTLD_NOW | RTLD_NOLOAD);
    }
  }
  *error = "QtWebEngineCore is not loaded";
  return nullptr;
#else
  void *library = dlopen("libQt6WebEngineCore.so.6", RTLD_NOW | RTLD_NOLOAD);
  if (!library) {
    *error = "libQt6WebEngineCore.so.6 is not loaded";
  }
  return library;
#endif
}

void *find_constructor(const char **error) {
  void *library = open_loaded_library(error);
  if (!library) {
    return nullptr;
  }
  void *ctor = dlsym(library, "_ZN17QWebEngineProfileC1EP7QObject");
  if (!ctor) {
    *error = "QWebEngineProfile constructor not found";
  }
  // PyQt keeps the library loaded, so the constructor stays valid.
  dlclose(library);
  return ctor;
}

#endif

} // namespace

// Returns a new parentless QWebEngineProfile, or null with *error set. The
// memory comes from malloc(), because Qt frees it with operator delete, which
// calls free() with MSVC, libstdc++ and libc++.
extern "C" void *anki_new_webengine_profile(const char **error) {
  void *ctor = find_constructor(error);
  if (!ctor) {
    return nullptr;
  }
  void *profile = malloc(kProfileSize);
  if (!profile) {
    *error = "out of memory";
    return nullptr;
  }
  reinterpret_cast<Constructor>(ctor)(profile, nullptr);
  return profile;
}
