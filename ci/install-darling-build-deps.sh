#!/usr/bin/env bash
set -euo pipefail

# Keep the hosted runner close to Darling's documented Debian build image.
packages=(
	bison
	clang
	cmake
	flex
	gcc-multilib
	libavcodec-dev
	libavformat-dev
	libavutil-dev
	libbsd-dev
	libboost-all-dev
	libc6-dev-i386
	libcairo2-dev
	libcap-dev
	libcap2-bin
	libdbus-1-dev
	libegl1-mesa-dev
	libelf-dev
	libfontconfig1-dev
	libfreetype6-dev
	libfuse-dev
	libgif-dev
	libgl1-mesa-dev
	libglu1-mesa-dev
	libjpeg-dev
	libpng-dev
	libpulse-dev
	libssl-dev
	libswresample-dev
	libtiff-dev
	libudev-dev
	libvulkan-dev
	libx11-dev
	libxcursor-dev
	libxkbfile-dev
	libxrandr-dev
	libxml2-dev
	llvm-dev
	ninja-build
	pkg-config
)

sudo apt-get update
sudo apt-get install --yes --no-install-recommends "${packages[@]}"
