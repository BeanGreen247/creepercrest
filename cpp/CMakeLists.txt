cmake_minimum_required(VERSION 3.13)
project(creepercrest CXX)

# Same options as the Makefile (ON/OFF, default OFF; the optimisation level is -O3 unless O2 is ON):
#   cmake -S . -B build/cmake -DO2=ON -DDISABLE_2FA=ON -DDISABLE_BACKUPS=ON -DDEBUG=ON
option(O2 "Use -O2 instead of -O3" OFF)
option(DISABLE_2FA "Password-only login, no authenticator code" OFF)
option(DISABLE_BACKUPS "Compile the backup feature out" OFF)
option(PANEL_POWER "Always show the panel Restart / Shut down buttons" OFF)
option(DEBUG "Debug build: -Og -g and request logging" OFF)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(OpenSSL REQUIRED)
find_package(ZLIB REQUIRED)
find_package(Threads REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(LIBZIP REQUIRED libzip)

# The UI is embedded with .incbin; the editor is gzip-compressed first, into build/ like the Makefile does.
set(EDITOR_GZ ${CMAKE_SOURCE_DIR}/build/editor.js.gz)
add_custom_command(
  OUTPUT ${EDITOR_GZ}
  COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_SOURCE_DIR}/build
  COMMAND sh -c "gzip -9 -n -c '${CMAKE_SOURCE_DIR}/ui/editor.js' > '${EDITOR_GZ}'"
  DEPENDS ${CMAKE_SOURCE_DIR}/ui/editor.js
  VERBATIM)
add_custom_target(editor_gz DEPENDS ${EDITOR_GZ})

file(GLOB SRC CONFIGURE_DEPENDS ${CMAKE_SOURCE_DIR}/src/*.cpp)
add_executable(creepercrest ${SRC})
add_dependencies(creepercrest editor_gz)
set_source_files_properties(${CMAKE_SOURCE_DIR}/src/assets.cpp PROPERTIES
  OBJECT_DEPENDS "${EDITOR_GZ};${CMAKE_SOURCE_DIR}/ui/index.html"
  COMPILE_FLAGS "-Wa,-I${CMAKE_SOURCE_DIR}")

if(DEBUG)
  set(OPT -Og -g)
  target_compile_definitions(creepercrest PRIVATE CC_DEBUG)
elseif(O2)
  set(OPT -O2)
else()
  set(OPT -O3)
endif()
if(DISABLE_2FA)
  target_compile_definitions(creepercrest PRIVATE CC_DISABLE_2FA)
endif()
if(DISABLE_BACKUPS)
  target_compile_definitions(creepercrest PRIVATE CC_DISABLE_BACKUPS)
endif()

if(PANEL_POWER)
  target_compile_definitions(creepercrest PRIVATE CC_PANEL_POWER)
endif()

# Ignore CMAKE_BUILD_TYPE so the flags match the Makefile build exactly.
set(CMAKE_CXX_FLAGS_RELEASE "")
set(CMAKE_CXX_FLAGS_DEBUG "")
target_compile_options(creepercrest PRIVATE ${OPT} -Wall -Wextra -fno-plt -fstack-protector-strong)
target_compile_definitions(creepercrest PRIVATE _FORTIFY_SOURCE=2)
target_include_directories(creepercrest PRIVATE ${CMAKE_SOURCE_DIR}/src ${LIBZIP_INCLUDE_DIRS})
target_link_options(creepercrest PRIVATE -Wl,-z,relro,-z,now)
target_link_libraries(creepercrest PRIVATE OpenSSL::SSL OpenSSL::Crypto ${LIBZIP_LIBRARIES} ZLIB::ZLIB Threads::Threads)
set_target_properties(creepercrest PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${CMAKE_SOURCE_DIR})
