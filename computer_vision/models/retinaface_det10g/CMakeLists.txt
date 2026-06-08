cmake_minimum_required(VERSION 3.10)
project(nvdsparsebbox_det10g)

find_package(CUDA REQUIRED)

set(DS_INCLUDE /opt/nvidia/deepstream/deepstream-8.0/sources/includes)

add_library(nvdsparsebbox_det10g SHARED nvdsparsebbox_det10g.cpp)

target_include_directories(nvdsparsebbox_det10g PRIVATE
    ${DS_INCLUDE}
    ${CUDA_INCLUDE_DIRS}
)

target_compile_options(nvdsparsebbox_det10g PRIVATE -fPIC -std=c++17)