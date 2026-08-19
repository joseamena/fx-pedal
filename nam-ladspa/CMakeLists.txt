cmake_minimum_required(VERSION 3.10)
project(nam_ladspa LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# Defaults to the pinned git submodule at nam-ladspa/NeuralAmpModelerCore.
# Override with -DNAM_CORE_DIR=/path/to/checkout to build against a
# different (e.g. newer) NeuralAmpModelerCore checkout instead.
set(NAM_CORE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/NeuralAmpModelerCore" CACHE PATH
    "Path to a NeuralAmpModelerCore checkout")

if(NOT EXISTS "${NAM_CORE_DIR}/NAM/dsp.h")
  message(FATAL_ERROR
    "NeuralAmpModelerCore not found at ${NAM_CORE_DIR}. If you cloned this repo "
    "without --recursive, run: git submodule update --init --recursive")
endif()

file(GLOB NAM_SOURCES_TOP "${NAM_CORE_DIR}/NAM/*.cpp")
file(GLOB NAM_SOURCES_SUB "${NAM_CORE_DIR}/NAM/*/*.cpp")
set(NAM_SOURCES ${NAM_SOURCES_TOP} ${NAM_SOURCES_SUB})

add_library(nam_ladspa SHARED nam_ladspa.cpp ${NAM_SOURCES})

target_include_directories(nam_ladspa PRIVATE "${NAM_CORE_DIR}")
target_include_directories(nam_ladspa SYSTEM PRIVATE
  "${NAM_CORE_DIR}/Dependencies/eigen"
  "${NAM_CORE_DIR}/Dependencies/nlohmann"
)

target_compile_definitions(nam_ladspa PRIVATE NAM_ENABLE_A2_FAST)
target_link_libraries(nam_ladspa PRIVATE stdc++fs)

set_target_properties(nam_ladspa PROPERTIES
  PREFIX ""
  OUTPUT_NAME "nam_ladspa"
  CXX_VISIBILITY_PRESET hidden
)

target_compile_options(nam_ladspa PRIVATE -O3 -Wall -Wextra -Wno-unused-parameter)
