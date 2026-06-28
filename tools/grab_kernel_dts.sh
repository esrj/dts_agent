#!/usr/bin/env bash
# Run this ON THE UBUNTU BUILD MACHINE (the one that runs bitbake/devtool).
# It extracts the EXACT kernel device-tree source the OpenSTLinux BSP builds
# (recipe: linux-stm32mp) and packages just the pieces the DTS patch agent needs.
#
# Usage:
#   1. Open the build terminal (the one whose prompt shows
#      build-openstlinuxrt-stm32mp2-rt, i.e. the Yocto env is already sourced).
#   2. Copy this script over and run:  bash grab_kernel_dts.sh
#   3. scp the resulting /tmp/stm32mp2-kernel-dts.tgz back to the Mac.
set -euo pipefail

OUT=/tmp/stm32mp2-kernel-dts.tgz

find_src() {
  # Preferred: devtool workspace (canonical, matches the BSP tutorial flow).
  local cand
  for cand in \
      "$PWD/workspace/sources/linux-stm32mp" \
      "$(ls -d "$PWD"/../*/workspace/sources/linux-stm32mp 2>/dev/null | head -1)" ; do
    [ -n "${cand:-}" ] && [ -d "$cand/arch/arm64/boot/dts/st" ] && { echo "$cand"; return; }
  done
  # Fallback: shared kernel source (no side effects on the recipe).
  for cand in $(ls -d "$PWD"/tmp*/work-shared/*/kernel-source 2>/dev/null); do
    [ -d "$cand/arch/arm64/boot/dts/st" ] && { echo "$cand"; return; }
  done
  return 1
}

SRC=$(find_src || true)

if [ -z "${SRC:-}" ]; then
  echo ">> Kernel source not unpacked yet. Running 'devtool modify linux-stm32mp'..."
  devtool modify linux-stm32mp
  SRC=$(find_src)
fi

echo ">> Using kernel source: $SRC"

# Provenance: exact commit the BSP builds.
{
  echo "recipe: linux-stm32mp"
  echo "source_path: $SRC"
  echo "git_head: $(git -C "$SRC" rev-parse HEAD 2>/dev/null || echo 'n/a')"
  echo "git_describe: $(git -C "$SRC" describe --tags --always 2>/dev/null || echo 'n/a')"
  echo "git_branch: $(git -C "$SRC" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'n/a')"
  echo "captured_on: $(uname -n)"
} > /tmp/stm32mp2-kernel-dts.provenance.txt
cat /tmp/stm32mp2-kernel-dts.provenance.txt

# Package only what the patch agent needs: the ST DTS dir + dt-bindings headers.
tar -C "$SRC" -czf "$OUT" \
    arch/arm64/boot/dts/st \
    include/dt-bindings \
    -C /tmp stm32mp2-kernel-dts.provenance.txt

echo
echo ">> DONE. Created: $OUT  ($(du -h "$OUT" | cut -f1))"
echo ">> From the Mac, pull it with e.g.:"
echo "   scp $(whoami)@$(hostname -I 2>/dev/null | awk '{print $1}'):$OUT ~/Desktop/DTS_patch_agent/"
