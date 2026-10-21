# HYISP util

A firmware flasher for keyboards built around the **HYISP** (Heng Yu ISP) USB HID
firmware-update bootloader. The bootloader is known from the **Das Keyboard 4** —
Professional, Ultimate and Professional for Mac — which report themselves as
`D4215` on USB `0x24F0` and are built by Heng Yu Technology as an OEM for Metadot.

This project targets the HYISP bootloader itself, not a single product: any
keyboard carrying it should work. Development and validation were done on the Das
Keyboard 4; other Das Keyboard models use different firmware and are not supported.
See [Scope](#scope) below.

The vendor ships flashing utilities for Windows and macOS. This is a single-file
cross-platform alternative with four additions the vendor tools do not have:

- **`flash-bootloader`** — updates the keyboard's 4 KB bootloader through a
  second flash path implemented by the running application firmware. The vendor
  tools (and the public static analyses of this protocol) assume no bootloader
  update route exists; the keyboard verifies the new image itself and refuses
  to boot it if it does not check out.
- **`verify`** — runs every non-writing check against a connected keyboard and
  reports a pass/fail table, so you can confirm a unit is updatable *before*
  starting a flash.
- **`info`** — parses and validates a firmware image without touching hardware.
- **`read`** — dumps the keyboard's flash image to a file, read-only, using a
  command neither vendor tool ever sends.

Both apply to both platforms; the vendor's Windows and macOS utilities are separate
programs that only run on their own OS.

## Install

```console
$ pip install hyisp-util
```

That is the whole command. `hidapi` is installed automatically as a dependency, and
the package installs the `hyisp-util` command.

On Linux, `hidapi` needs the USB system libraries present at runtime. Wheels are
published for macOS and Windows, so no extra work is needed there; on Linux
distributions that do not ship a wheel, install the runtime libraries first:

| Distribution | Command |
|---|---|
| Debian, Ubuntu | `sudo apt install libusb-1.0-0 libudev1` |
| Fedora, RHEL | `sudo dnf install libusbx systemd-libs` |
| Arch | `sudo pacman -S libusb libsystemd` |
| macOS | `brew install libusb` |

If `pip` tries to compile `hidapi` from source instead of using a wheel, you will
also need the headers (`libusb-1.0-0-dev libudev-dev` on Debian/Ubuntu,
`libusbx-devel libudev-devel` on Fedora) and a compiler.

You may need to be a member of a group that can read USB devices — on Debian and
Ubuntu that is typically `plugdev`:

```console
$ sudo usermod -aG plugdev $USER
```

Log out and back in for that to take effect. Without it the keyboard will not
enumerate for a non-root user even though it is plugged in.

## Scope

Supports devices that run the **HYISP** (Heng Yu ISP) firmware-update bootloader.
Development and validation were done on the **Das Keyboard 4** family:

| Model | USB | Notes |
|---|---|---|
| Das Keyboard 4 Professional | `0x24F0` | |
| Das Keyboard 4 Ultimate | `0x24F0` | |
| Das Keyboard 4 Professional for Mac | `0x24F0` | ships the macOS keymap variant |

All three present as `D4215` and share one firmware family. Other Das Keyboard
models (DK8, DK10, DK Pro, cloud-connected models, and any later release) are
**not** supported — they use different firmware and a different update mechanism.
Another keyboard that happens to carry the same HYISP bootloader is untested;
running this against it is not prevented by design, and it will simply fail to find
a device or fail validation.

**Firmware images are not distributed with this package.** Obtain them from the
vendor's official download. The image is a Windows or macOS variant and the two are
*not* interchangeable — the hardware is identical but the keycodes differ, and the
keyboard will not warn you if you flash the wrong one.

Note that the DK4 is a wired USB keyboard. Cloud-connected Das Keyboard models
require the vendor's DDK utility over a network connection and are unrelated to
this protocol.

### Bare and community firmware

Community DK4 projects ship their builds as plain, unpadded ARM binaries with no
metadata, which the keyboard will not accept: it expects a whole-KB image, it
deobfuscates flash as it programs it, and it validates a checksum in the image
trailer before it will boot. `convert` wraps such an image in that container —
padding, metadata and checksum — producing a file that is structurally identical
to a vendor image, so `flash` and the vendor's own tools both accept it:

```console
$ hyisp-util convert bare_firmware.bin
$ hyisp-util flash bare_firmware.bin.vendor.bin --yes
```

It touches no hardware, and it reports the resulting size, profile and checksum
so the output can be checked with `info` and `selftest` before flashing.

To skip the intermediate file, `--convert` wraps the image in memory and uses it
directly. `flash`, `verify` and `selftest` all accept it, and nothing is written
to disk:

```console
$ hyisp-util verify bare_firmware.bin --convert --no-latch
$ hyisp-util flash bare_firmware.bin --convert --yes
```

Without the flag a bare image is refused with the size error and this hint, since
the two forms are not interchangeable and guessing would erase the wrong bytes.
Use `convert` when you want to keep the wrapped image — to archive it, inspect it
with `info`, or flash it later with the vendor's own tool.

### Updating the bootloader

The keyboard's bootloader (LDROM) can be updated while the keyboard runs
normally, through a second flash path the application firmware implements.
Both stages erase and program only *the other* stage's region, so a bootloader
update can never overwrite the running application:

```console
$ hyisp-util flash-bootloader A2175V13.bin
```

The image is a 4 KB bootloader image, either a vendor container (sent
verbatim) or a plaintext dump read back from flash (obfuscated in memory). The
keyboard must be in normal (application) mode. The device authenticates,
erases, programs and latches the new image, then verifies it itself — HYISP
signature and checksum — and only reboots if it checks out; a failed verify
leaves the keyboard running the application and the write can simply be
retried. After a successful update the new bootloader is entered once to
confirm it boots, and the keyboard is returned to application mode
(`--stay-in-isp` keeps it in bootloader mode instead).

## Usage

```console
$ hyisp-util list                      # find connected keyboards
$ hyisp-util info firmware.bin         # inspect an image, no hardware
$ hyisp-util selftest firmware.bin     # exercise the protocol vs a mock
$ hyisp-util convert bare_firmware.bin # wrap a bare image for flashing
$ hyisp-util flash firmware.bin --dry-run
$ hyisp-util flash firmware.bin --yes
$ hyisp-util flash-bootloader bootloader.bin --yes  # update the bootloader
$ hyisp-util verify firmware.bin       # check a unit is updatable
$ hyisp-util probe                     # query versions, read-only
$ hyisp-util regs                      # CONFIG0/CONFIG1 dump, read-only
$ hyisp-util reset                     # back to application mode
$ hyisp-util read -o flash_dump.bin     # dump flash (read-only)
$ hyisp-util flash bare_firmware.bin --convert   # wrap in memory
```

Run any subcommand with `-h` for its options.

### Read-only diagnostics

`probe` and `regs` never erase, write or lock anything, so they are safe to run
on a keyboard that is in service:

```console
$ hyisp-util probe
$ hyisp-util regs
device 0x24F0:0140 Das Keyboard (application mode)
  CONFIG0 (0x00300000) = 0xFFFFFF78
    CWDTEN=1 (WWDT disabled at power-on)
    CFOSC=111 (internal 22.1184 MHz (RC))
    CBODEN=1 (BOD disabled after POR)
    CBOV=11
    CBORST=1 (BOD reset disabled)
    CBS=01 (boot LDROM)
    DFVSEN=0 (variable size via DFBADR)
    LOCK=0 (locked (ICP/debug readout blocked))
    (note: on a DK4 the raw word reads 0xFFFFFF78; the CBS=01 decode means POR enters the LDROM, which then chains to the app -- not a contradiction. Decode is per-TRM.)
  CONFIG1 (0x00300004) = 0x00008000 (DFBADR=0x8000)
  GCR PDID  (0x50000000) = 0x00012315 (payload 00 01 23 15)
  flash base (0x2000013C) = 0x00008000
```

`regs` reads the chip's user-configuration words and decodes them, which is
how you see the flash **LOCK** state and the **CBS** boot-origin bits — the two
things that decide whether the keyboard boots from its bootloader or from its
application, and whether its flash can be read back over the debug interface.
This is an **application-mode** command: the bootloader implements none of these
getters and answers `0xF1` to `0xFB`. Run it against a unit sitting in
application mode; `--allow-isp` only makes the tool *find* a unit that is
already in bootloader mode, where the read then fails with a clear error.

### Typical session

```console
$ hyisp-util info L1947V33.bin
L1947V33.bin: application, 32 KB, profile Global Full
...
entry command  0xA0
version        'S1947V33'
  variant      Windows firmware, enumerates as 0x24F0:0140

$ hyisp-util verify L1947V33.bin
PASS: 16/16 checks passed
application version: 'S1947V33'
bootloader version: 'B2175V13'

$ hyisp-util flash L1947V33.bin --yes
  0xA0 enter ISP
  device reports target 32 KB, model 0x0000
  ...
Update successfully.
```

## Safety notes

- **Always run `verify` before `flash`.** It reports whether the unit can accept the
  image, including the profile check that actually gates the update.
- **Pass `--yes` deliberately.** `flash` is interactive by default; `--yes` skips the
  confirmation and is what makes it non-interactive.
- **Interrupted writes fail safe.** The image checksum is validated by the
  keyboard itself, and this tool writes the vector table last, so a flash cut short
  by a cable pull will not leave the unit unbootable.
- **Wrong-OS firmware is not caught by the device.** Both Windows and macOS images
  carry the same profile, so the keyboard accepts either. The symptom is misplaced
  keys, not a failure. `flash` warns when the running firmware disagrees with the
  image's known variant.
- **Bootloader updates are verify-gated by the device.** `flash-bootloader`
  writes the 4 KB bootloader through the application's flash path, and the
  keyboard itself confirms the new image's HYISP signature and checksum before
  it reboots into it. A rejected image leaves the keyboard running the
  application, with the bootloader erased — re-run the command with a
  known-good image. `flash` still refuses bootloader images, because from
  bootloader mode those commands would address the application window instead.
- **`verify` writes no flash** but does enter bootloader mode, and by default
  exercises a latch it then recovers from. Pass `--no-latch` to skip that part.
- **`read` writes no flash** but does enter bootloader mode, and resets the
  keyboard back to normal operation when it finishes. Pass `--stay-in-isp` to
  keep it in bootloader mode; `hyisp-util reset` brings it back.
- **`flash --dry-run` still enters bootloader mode** and skips the reset that
  returns the keyboard to normal operation. If it stops there, run
  `hyisp-util reset` or unplug and replug.

## Troubleshooting

**`error: no HYISP device interface found`** — check `list`. If the keyboard is in bootloader
mode it shows PID `0x0000`; run `reset` to return it to normal operation.

**`OSError: read error` right after a mode change** — expected. The keyboard
re-enumerates and hidapi reports the drop as an error. The tool retries on its own;
a persistent failure means the keyboard did not come back, so replug it.

**Update fails at `0xA4 erase chip` with status `0x18`** — the profile check that
preceded it was rejected, even though the tool may have looked past it. This leaves
the existing firmware intact.

**The keyboard is unresponsive to commands but still types** — it is latched in
bootloader mode. `reset` fixes it without a power cycle.

## How it works

The [repository](https://github.com/giannello/hyisp-util) contains
`FIRMWARE_UPDATE_PROTOCOL.md`, which documents the wire protocol in full: frame
format, command table, firmware image layout and checksum, and the update
sequence, with the behavioural details that were measured on hardware and differ
from what the vendor's own tools do. Section numbers referenced in error messages
and source comments refer to that document.

That document is not shipped in the installed package — it is in the repository
only.

## AI disclosure

See AI_DISCLOSURE.md.

## License

GNU General Public License v3
See [`COPYING`](COPYING) for the full text.

    Copyright (C) 2026 Giuseppe Iannello

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, version 3 of the License.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.
