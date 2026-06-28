# STM32MP257F-EV1 — Complete Baseline DTS Source Set (Kernel DT)

This is the **complete, buildable kernel device-tree source** for the
STM32MP257F-EV1 board, taken **byte-for-byte from the OpenSTLinux BSP that
Yocto actually builds** (recipe `linux-stm32mp`). All generated DTS for the
patch agent is produced by patching these files; the BSP integrates the result
as a `.patch` via `devtool` / `bbappend` (`SRC_URI += "file://..."`).

## Provenance — pulled from the build machine

Captured with `tools/grab_kernel_dts.sh` from the Yocto build host's
`devtool modify linux-stm32mp` workspace:

- **Recipe**: `linux-stm32mp` (OpenSTLinux kernel)
- **git head**: `5d10a6dfaf5964bf12b2e77d2761a8c205bca25c`
- **git describe**: `devtool-patched-43-g5d10a6dfa`  (ST kernel base + 43 recipe patches applied — i.e. the real built tree)
- **branch**: `devtool`
- **Build host**: `ubuntu-gnu-linux-24-04-3` (Parallels VM)
- **Source path**: `.../build-openstlinuxrt-stm32mp2-rt/workspace/sources/linux-stm32mp`
- **BSP**: MACHINE `stm32mp2-rt`, DISTRO `openstlinux-rt` 5.0.15-snapshot-20260523
- **Arch path upstream**: `arch/arm64/boot/dts/st/`

Raw capture details in `.source_provenance.txt`.

> NOTE: this replaces an earlier mainline (`torvalds/master`) snapshot. Mainline
> differed from the BSP (e.g. it lacked `stm32mp257f-ev1-resmem.dtsi`), so a
> patch generated against it would not apply cleanly to the BSP. This ST set is
> the one to build/patch against.

## Processing applied on import (so the baseline is clean)

1. **Stripped a contaminating `stm-agent` managed region** that a previous
   patch-agent run had appended to `stm32mp257f-ev1.dts` (an auto-generated
   `&csi` block, lines 929–942). The pristine board file is 928 lines. The
   stale `stm32mp257f-ev1.dts.stm-agent.bak` was dropped. Verified no other
   file in the tree carried agent markers.
2. **Resolved a dangling symlink**: `dt-bindings/input/linux-event-codes.h` is a
   symlink to `include/uapi/linux/input-event-codes.h`, which lay outside the
   `dt-bindings/` dir that was tarred. The real file (kernel-stable, not
   ST-patched) was fetched from `STMicroelectronics/linux@v6.6-stm32mp` and
   placed inline at that path.

## Include tree (top → leaves)

```
stm32mp257f-ev1.dts            board top file (928 lines, pristine)
├── <dt-bindings/...>          macro/constant headers (see include/)
├── stm32mp257.dtsi → stm32mp255 → stm32mp253 → stm32mp251.dtsi   SoC base (peripheral nodes, e.g. usart2: serial@400e0000)
├── stm32mp25xf.dtsi           "F" crypto-enabled variant
├── stm32mp25-pinctrl.dtsi     pinctrl GROUPS (STM32_PINMUX entries)
├── stm32mp25xxai-pinctrl.dtsi pinctrl for the AI package variant
└── stm32mp257f-ev1-resmem.dtsi  ST reserved-memory layout (ST-specific, not in mainline)
```

9 local source files + 13 `dt-bindings/` headers.

### Localization landmarks (for plan.md Stage 2)

- Peripheral nodes (labels like `&usart2`): defined in `stm32mp251.dtsi`
- Pinctrl groups (labels like `&usart2_pins_a`): defined in `stm32mp25-pinctrl.dtsi`
- Board-level node enablement / overrides (`&usart2`, `&spi3`, `&csi`, ...):
  in `stm32mp257f-ev1.dts` itself

## Validation status

- `cpp` preprocess (`gcc -E`): **PASS** — all includes resolve, 0 unresolved
  `#include`, `STM32_PINMUX(...)` expands, 6208 lines preprocessed.
- `dtc` compile: **not run** — `dtc` not installed on this Mac. Install to enable
  Stage 4 build checks: `brew install dtc`.

### Reproduce / re-validate locally

```sh
cd data/stm32mp257f-ev1/baseline/dts
gcc -E -nostdinc -undef -D__DTS__ -x assembler-with-cpp \
    -I include stm32mp257f-ev1.dts -o /tmp/ev1.preprocessed.dts
# then (once dtc is installed):
# dtc -I dts -O dtb -o /tmp/ev1.dtb /tmp/ev1.preprocessed.dts
```

## Re-pulling a fresh copy from the build machine

Run `tools/grab_kernel_dts.sh` on the Yocto build host, scp the resulting
tarball back, then re-apply the two import steps above (strip managed region +
resolve the input-event-codes symlink).
