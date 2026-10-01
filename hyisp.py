#!/usr/bin/env python3
# HYISP (Heng Yu ISP) firmware flasher.
# Copyright (C) 2026 Giuseppe Iannello
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, version 3 of the License.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# SPDX-License-Identifier: GPL-3.0-only
"""
HYISP (Heng Yu ISP) firmware flasher.

Implements the USB HID vendor firmware-update protocol used by the Heng Yu ISP
bootloader, as found on the Metadot Das Keyboard 4 — Professional, Ultimate and
Professional for Mac — which report themselves as `D4215` on USB `0x24F0`.  The
full write-up, `FIRMWARE_UPDATE_PROTOCOL.md`, lives in the repository rather than
in this package; section numbers in the comments below refer to it.  All
behavioural claims were confirmed against physical hardware.

The transport is the more robust of the two vendor implementations: 8-byte write
chunks with 3 retries each, and a two-pass write ordering that commits the
vector-table region last so an interrupted flash cannot leave the device unbootable.
The `0xA5` profile check is sent *raw*, which is the encoding the bootloader
accepts for its own profile pool; each firmware stage accepts the
nibble-swapped/inverted transform (§5.3) only of *its own* pool string, so a
payload that works against one stage is refused with `0x18` by the other.

The bootloader itself can be updated with `flash-bootloader`: the application
firmware implements a second flash path whose commands address the 4 KB LDROM
window, so a new bootloader image is written while the keyboard runs normally
from application mode.  Both stages erase/program only the *other* stage's
region, and the device verifies the new image (HYISP signature + checksum)
before it reboots.

Usage:
    hyisp-util list
    hyisp-util info firmware.bin
    hyisp-util selftest firmware.bin   # no hardware; mock device
    hyisp-util convert bare_firmware.bin
    hyisp-util flash firmware.bin --dry-run
    hyisp-util flash firmware.bin
    hyisp-util flash bare_firmware.bin --convert   # wrap in memory
    hyisp-util flash-bootloader bootloader.bin     # update LDROM from app mode
    hyisp-util verify firmware.bin   # check a unit is updatable
    hyisp-util probe                 # query versions, read-only
    hyisp-util regs                  # CONFIG0/CONFIG1 register dump, read-only
    hyisp-util reset                 # back to application mode
    hyisp-util read -o flash_dump.bin   # dump flash (read-only)

Developed and validated on the Das Keyboard 4 family (Professional, Ultimate and
Professional for Mac) -- all present as `D4215` on USB `0x24F0`.  Any other
keyboard built around the same HYISP bootloader should work but is untested;
other Das Keyboard models use different firmware and are not supported.

Firmware images are not distributed with this package; obtain them from the
vendor's official download.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPORT_SIZE = 8  # 1 report-ID byte + 7 data bytes (§2)
REPORT_ID = 0x01
DATA_PER_REPORT = REPORT_SIZE - 1

# Response frames are longer than a single report: 0xB0 answers with a 13-byte
# frame, which a live keyboard returns whole in one feature-report read when
# asked for more than 8 bytes.  64 leaves headroom for the longest reply.
FEATURE_BUF_SIZE = 64

REQ_MAGIC = 0xEA
RSP_MAGIC = 0xED

# Commands (§4)
CMD_TEST_MODE = 0x00
CMD_ENTER_ISP = 0xA0
CMD_WRITE_FLASH = 0xA1
CMD_ERASE_CHIP = 0xA4
CMD_CHECK_PROFILE = 0xA5
CMD_ISP_VERSION = 0xA6
CMD_READ_FLASH = 0xA7
CMD_PROTECT_CHIP = 0xA8
CMD_ENTER_LDROM_ISP = 0xAA
CMD_RESET = 0xAF
CMD_APP_VERSION = 0xB0

# Read-only register getters (§10.4).  0xFB emits read32(0x00300000) and
# read32(0x00300004) MSB-first; 0xB8 returns read32(0x50000000) (GCR, PDID)
# byte-pair-swapped; 0xB9 returns the RAM flash-base global big-endian.
CMD_USER_CONFIG = 0xFB
CMD_READ_PDID = 0xB8
CMD_FLASH_BASE = 0xB9

# Measured on a real DK4 (bootloader B2175V13): 0xA5 returns 0x18 for any profile
# it does not accept, and a rejected profile makes the later 0xA4 erase fail
# with the same status.  The device requires at least 8 bytes before it compares.
PROFILE_CHECK_REJECTED = 0x18
PROFILE_MIN_LEN = 8

# Vendor IDs the Windows tool accepts (§1).  The macOS tool omits 0x058F.
KNOWN_VENDORS = {0x058F, 0x0F39, 0x0A34, 0x24F0}

# Usage page 0x01 (Generic Desktop) / usage 0x80 selects the vendor control
# interface rather than the boot-keyboard interface (§1).
HID_USAGE_PAGE = 0x01
HID_USAGE = 0x80

# ISP images are exactly 4 KB and self-identify via the HYISP profile (§5.1).
ISP_IMAGE_SIZE = 0x1000

# An application image is padded out to the whole-KB size the device advertises
# in its 0xA0 response, which measured 32 KB on a DK4 (§6.3).  That byte is a
# per-mode target size, not a capacity check (§6.5), so `convert` takes it as an
# option rather than discovering it.
APPLICATION_IMAGE_SIZE = 32 * 1024

# The 16-byte trailer (§5.2): "HY" + 2-byte region + 5-byte image identifier +
# 2 reserved bytes + image-type flag, followed by a little-endian uint32
# checksum.  0x00 is the application type flag; the ISP loader uses 'U' (0x55).
TRAILER_SIZE = 16
APPLICATION_TRAILER_PREFIX = b"HYGFD4215\x00\x00\x00"

# Write geometry.  The vector-table region is committed in a second pass (§6
# steps 10-11, §7.4).
BLOCK_SIZE = 256
VECTOR_TABLE_BYTES = 0x20 * BLOCK_SIZE  # 0x2000 -- first pass skips to 0x100
DEFAULT_CHUNK = 8
RETRIES_PER_CHUNK = 3

# The application-side bootloader update (flash-bootloader) was measured with
# 64-byte A1 payloads; larger than the app-image write geometry because that
# path runs from application mode, whose frame parser accepts the full count.
BOOTLOADER_WRITE_CHUNK = 64

# 0xA5 auth payload for the application-side flash path: the running
# application accepts plaintext "GF" at payload[0..1] (the LDROM's 0xA5 wants
# the obfuscated profile trailer instead -- different pool, same protocol).
APP_AUTH_PAYLOAD = b"GF"

# Read geometry for 0xA7 (§6.6).  A response frame is count + 5 bytes and the
# keyboard hands over at most 15 data bytes per feature-report read, so
# READ_CHUNK <= 10 keeps every reply inside one read; a larger count spans reads
# and can be sampled mid-fill, which fails the XOR checksum -- hence the retries.
READ_CHUNK = 10
READ_RETRIES = 6

RECV_TIMEOUT = 5.0  # seconds; Windows WaitForSingleObject(evt, 5000)

# The "ISP Mode" sentinel is PID 0 on a recognised vendor (§1).  The
# bootloader image shipped in the macOS update package (A2175V13.bin) carries
# idProduct 0x0001 instead of 0x0000 -- the only difference from the Windows
# image in the whole 4 KB -- so accept both when classifying ISP-mode
# devices; both report the product string "HYISP".
ISP_MODE_PID = 0x0000
ISP_MODE_PIDS = frozenset({0x0000, 0x0001})

# Application PIDs are not fixed: they vary with the running firmware variant.
# Measured on a DK4 by flashing two images and re-reading the descriptor:
#   S1947V33 -> 0x24F0:0x0140
#   S2175V16 -> 0x24F0:0x0141
# Discovery keys off the vendor ID and usage, not the PID (§1), so these only
# matter where something asserts "this is an application-mode keyboard".
APP_PIDS = frozenset({0x0140, 0x0141})


class FlashError(Exception):
    """Any recoverable flashing failure.  Carries a user-facing message."""


# ---------------------------------------------------------------------------
# Obfuscation (§5.0)
# ---------------------------------------------------------------------------


def deobfuscate_byte(b: int) -> int:
    """Invert all bits, then swap nibbles.  Involutive: obf(x) == deob(x)."""
    hi, lo = b >> 4, b & 0xF
    return (((~lo) & 0xF) << 4) | ((~hi) & 0xF)


def deobfuscate(data: bytes) -> bytes:
    return bytes(deobfuscate_byte(b) for b in data)


def obfuscate(data: bytes) -> bytes:
    """The same transform.  Inversion and nibble-swapping are both involutions
    and commute, so the obfuscation is its own inverse (§5.0)."""
    return deobfuscate(data)


def swap_nibbles(x: int) -> int:
    return ((x << 4) | (x >> 4)) & 0xFF


def image_checksum(deob_body: bytes) -> int:
    """Little-endian two's-complement 32-bit word sum, so the total is zero (§5.4).

    Note this runs over the *deobfuscated* body -- the device validates it after
    deobfuscation, not the tool.
    """
    if len(deob_body) % 4:
        raise FlashError(f"image body is not a multiple of 4 bytes ({len(deob_body)})")
    total = sum(struct.unpack("<%dI" % (len(deob_body) // 4), deob_body)) & 0xFFFFFFFF
    return (0 - total) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Frame codec (§3)
# ---------------------------------------------------------------------------


def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    """Build a request frame: [0xEA][len][cmd][payload...][XOR8].

    `len` covers bytes 2..len inclusive, i.e. cmd + payload + checksum byte.
    """
    if len(payload) > 0xFF:
        raise FlashError("payload too long for one frame")
    length = 1 + len(payload) + 1
    body = bytes((REQ_MAGIC, length, cmd)) + payload
    return body + bytes((_xor8(body),))


def parse_response(frame: bytes) -> tuple[int, int, bytes]:
    """Split a response frame into (cmd, status, payload).

    Layout: [0xED][len][cmd][status][payload...][XOR8]
    """
    if len(frame) < 4:
        raise FlashError(f"response too short: {frame.hex()}")
    if frame[0] != RSP_MAGIC:
        raise FlashError(f"bad response magic 0x{frame[0]:02X} (want 0x{RSP_MAGIC:02X})")
    length = frame[1]
    if len(frame) != length + 2:
        raise FlashError(
            f"response length mismatch: header says {length + 2} bytes, got {len(frame)}"
        )
    expected = _xor8(frame[: length + 1])
    if frame[-1] != expected:
        raise FlashError(
            f"checksum mismatch: got 0x{frame[-1]:02X}, expected 0x{expected:02X}"
        )
    return frame[2], frame[3], frame[4 : length + 1]


def _xor8(data: bytes) -> int:
    acc = 0
    for b in data:
        acc ^= b
    return acc


# ---------------------------------------------------------------------------
# Firmware image parsing (§5.1, §5.2)
# ---------------------------------------------------------------------------

PROFILE_NAMES = {
    b"GF": "Global Full",
    b"GL": "Global Lite",
    b"CL": "Chinese Lite",
}

# The device answers 0xA5 by echoing the *transformed* profile bytes, so the
# response is looked up by transformed pair rather than by deobfuscating it
# (§5.3).  Built from the transform so it cannot drift out of sync.
TRANSFORMED_TO_PROFILE: dict[bytes, bytes] = {}


def _init_transform_table() -> None:
    for plain in PROFILE_NAMES:
        TRANSFORMED_TO_PROFILE[
            bytes((swap_nibbles(deobfuscate_byte(plain[0])),
                   deobfuscate_byte(deobfuscate_byte(plain[1]))))
        ] = plain


_init_transform_table()


@dataclass
class Firmware:
    path: str
    raw: bytes
    is_isp: bool
    profile: bytes
    magic: bytes
    identifier: bytes
    type_flag: int
    stored_checksum: int
    calculated_checksum: int
    source_path: str = ""
    """Set when `path` was built in memory from a bare image at `source_path`."""

    @property
    def size(self) -> int:
        return len(self.raw)

    @property
    def region(self) -> str:
        return PROFILE_NAMES.get(self.profile[:2], "unknown")

    @property
    def kind(self) -> str:
        return "bootloader (ISP)" if self.is_isp else "application"

    @property
    def checksum_ok(self) -> bool:
        return self.stored_checksum == self.calculated_checksum


# Version strings live obfuscated in the image body, not the trailer: `S1947V33`
# sits at 0x0B08 in L1947V33.bin and `S2175V16` at 0x0ADC in L2175V16.bin.
VERSION_RE = re.compile(rb"[A-Z][0-9]{4}V[0-9]{2}")

# Provenance: Metadot ships a separate flashing utility per OS, and each archive
# carries its matching firmware image.  The Windows archive contains
# L1947V33.bin (S1947V33); the macOS archive contains L2175V16.bin (S2175V16).
# The hardware is identical -- only the emitted keycodes differ.
#
# The PID half is measured, not extracted: the value is not reliably locatable in
# the obfuscated body (`40 01` occurs 8+ times in L1947V33.bin), so each entry
# was observed by flashing the image and re-reading the USB descriptor.
KNOWN_VARIANTS = {
    "S1947V33": (0x0140, "Windows"),
    "S2175V16": (0x0141, "macOS"),
    "B2175V13": (0x0000, "bootloader/ISP"),
}


def image_version_string(fw: Firmware) -> str:
    """Best-effort version string deobfuscated out of the image body."""
    plain = bytes(deobfuscate_byte(b) for b in fw.raw)
    found = [m.group(0).decode("ascii") for m in VERSION_RE.finditer(plain)]
    for candidate in found:
        if candidate in KNOWN_VARIANTS:
            return candidate
    return found[0] if found else "<unknown>"


def read_image(path: str) -> bytes:
    with open(path, "rb") as fh:
        raw = fh.read()
    if not raw:
        raise FlashError(f"{path} is empty")
    return raw


def load_firmware(path: str) -> Firmware:
    return parse_firmware(path, read_image(path))


def parse_firmware(path: str, raw: bytes) -> Firmware:
    """Validate and decode an image that is already in the vendor container."""
    if len(raw) % 1024:
        hint = ""
        if looks_like_bare_image(raw):
            hint = (
                "\nThis looks like a bare (unobfuscated, unpadded) firmware image. "
                "Wrap it in the vendor container first:\n"
                f"  hyisp-util convert {path}\n"
                f"or pass --convert to flash/verify it directly."
            )
        raise FlashError(
            f"file size {len(raw)} is not a multiple of 1024 "
            f"(the device only accepts whole-KB images){hint}"
        )
    return _parse_vendor_image(path, raw)


def _parse_vendor_image(path: str, raw: bytes) -> Firmware:
    # Trailer: 16 bytes at the end.  All of it except the checksum is obfuscated.
    trailer = deobfuscate(raw[-16:])
    magic = trailer[0:2]
    profile = trailer[2:12]
    stored = struct.unpack("<I", trailer[12:16])[0]

    identifier = profile[2:7]
    type_flag = profile[9]

    # File-type decision (§5.1).
    if len(raw) == ISP_IMAGE_SIZE:
        # Read the signature positionally rather than trusting the trailer slice.
        sig = deobfuscate(raw[0xFF4:0xFF9])
        if sig != b"HYISP":
            raise FlashError(
                "The firmware file is not a valid ISP firmware file. "
                f"(expected HYISP at 0xFF4, got {sig!r})"
            )
        is_isp = True
    else:
        if magic[:2] != b"HY":
            raise FlashError(
                "The firmware file is not a valid firmware file. "
                f"(expected HY magic at size-16, got {magic[:2]!r})"
            )
        is_isp = False

    if magic != b"HY":
        raise FlashError(f"bad magic in trailer: {magic!r} (want b'HY')")

    # Sanity-check the declared image type against the size-based decision.
    declared_isp = identifier == b"HYISP"
    if declared_isp != is_isp:
        raise FlashError(
            f"inconsistent image: size implies {'ISP' if is_isp else 'application'} "
            f"but the profile says {identifier!r}"
        )

    # The checksum field is stored obfuscated as well; deobfuscate the whole
    # image, then split (§5.4).
    deob_all = deobfuscate(raw)
    return Firmware(
        path=path,
        raw=raw,
        is_isp=is_isp,
        profile=profile,
        magic=magic,
        identifier=identifier,
        type_flag=type_flag,
        stored_checksum=stored,
        calculated_checksum=image_checksum(deob_all[:-4]),
    )


def looks_like_bare_image(data: bytes) -> bool:
    """True if `data` starts with a plausible ARM Cortex-M vector table.

    Used only to catch the mistake of pointing `convert` at something that is
    not firmware at all: word 0 is the initial stack pointer (SRAM) and word 1
    is the reset vector, which always has the Thumb bit set.
    """
    if len(data) < 8:
        return False
    sp, reset = struct.unpack_from("<II", data, 0)
    return 0x20000000 <= sp < 0x40000000 and (reset & 1)


def build_vendor_image(code: bytes, size: int = APPLICATION_IMAGE_SIZE) -> bytes:
    """Wrap a bare firmware image in the vendor container (§5.0, §5.2, §5.4).

    Community/open-source DK4 builds ship as plain, unpadded ARM binaries with
    no metadata.  They are not flashable as they stand, and padding them is not
    enough: the updater transmits file bytes verbatim and the device
    deobfuscates flash in place after programming (§5.0), so the bytes that reach
    flash have to be built exactly the way the vendor builds them.  In order:

    - pad with `0xFF` **before** obfuscation, which is `0x00` in the file.  Erased
      flash is `0xFF` and both vendor application images pad with it, so the
      deobfuscated image stays byte-identical to the file and the checksum keeps
      balancing.  Padding with `0x00` instead would be written as `0xFF` and
      deobfuscate to `0x00` -- the writer skips blocks that match the erased
      state, so the two are not interchangeable.
    - append the trailer, with the application type flag `0x00` (§5.2).
    - append the little-endian checksum, chosen so the uint32 sum of the
      deobfuscated body is zero (§5.4).
    - obfuscate the whole image, because that is the form the device stores.

    The result is indistinguishable in structure from a vendor application
    image, so `flash` and the vendor tools both accept it unchanged.
    """
    if size % 1024:
        raise FlashError(f"target size {size} is not a multiple of 1024")
    if not code:
        raise FlashError("firmware image is empty")
    if len(code) % 4:
        raise FlashError(
            f"firmware is {len(code)} bytes, not a multiple of 4; the checksum "
            f"is a uint32 sum, so the image has to be 4-byte aligned"
        )
    if len(code) > size - TRAILER_SIZE:
        raise FlashError(
            f"firmware is {len(code)} bytes, which does not fit in a "
            f"{size // 1024} KB image ({size - TRAILER_SIZE} usable bytes)"
        )
    plain = (
        code
        + b"\xff" * (size - TRAILER_SIZE - len(code))
        + APPLICATION_TRAILER_PREFIX
    )
    return obfuscate(plain + struct.pack("<I", image_checksum(plain)))


def wrap_bare_image(
    path: str,
    size: int = APPLICATION_IMAGE_SIZE,
    force: bool = False,
) -> Firmware:
    """Read a bare firmware file and wrap it in the vendor container, in memory.

    This is the whole of `convert` as far as the rest of the tool is concerned:
    everything downstream works from `fw.raw`, so an image that is wrapped this
    way behaves exactly like one loaded from disk, and nothing is written to disk
    unless the user asks for it with `convert`.

    `force` skips the vector-table check, which only guards against being handed
    a file that is not firmware at all.
    """
    code = read_image(path)
    if not force and not looks_like_bare_image(code):
        # Checked before the size heuristic, because a bare image is allowed to
        # be a whole number of KB and would otherwise be misread as wrapped.
        if len(code) % 1024 == 0:
            raise FlashError(
                f"{path} is {len(code)} bytes, a whole number of KB, and does "
                "not begin with a Cortex-M vector table -- so it looks like it is "
                "already in the vendor container. Drop --convert to use it as-is."
            )
        raise FlashError(
            f"{path} does not begin with a Cortex-M vector table (word 0 should "
            "be a stack pointer in SRAM, word 1 a reset vector with the Thumb "
            "bit set), so it does not look like a bare firmware image.\n"
            "If it is already a vendor image it needs no conversion -- check it "
            "with `info`."
        )
    raw = build_vendor_image(code, size)
    fw = _parse_vendor_image(path, raw)
    # `path` stays the file the user named, but `raw` is what every later step
    # sees, so record the provenance for `describe_image` to surface.
    fw.source_path = path
    return fw


def describe_image(fw: Firmware) -> str:
    """One line naming the image, flagging it if it was wrapped in memory."""
    origin = f" [wrapped from {fw.source_path} in memory]" if fw.source_path else ""
    return f"{fw.path}: {fw.size} bytes, {fw.kind}, {fw.region}{origin}"


def build_profile_payload(profile_obf: bytes) -> bytes:
    """Transform the 10 obfuscated profile bytes the way the device expects (§5.3).

    The tool's transform is deliberately asymmetric and this reproduces it
    byte-for-byte rather than "fixing" it:

        out[0]    = swap_nibbles(in[0])            # NOT inverted
        out[1..9] = ~swap_nibbles(in[i]) == deob(in[i])

    out[0] therefore equals ~deob(in[0]), i.e. the device compares byte 0
    against a pre-deobfuscation value while bytes 1-9 are post-deobfuscation.
    """
    if len(profile_obf) != 10:
        raise FlashError(f"profile block must be 10 bytes, got {len(profile_obf)}")
    out = [swap_nibbles(profile_obf[0])]
    for b in profile_obf[1:]:
        out.append(deobfuscate_byte(b))
    return bytes(out)


def profile_name_from_response(payload: bytes) -> str:
    """Decode the device's 2-byte profile answer into a display name (§5.3).

    The device echoes the transformed bytes (out[0], out[1]), so this is a table
    lookup, not a deobfuscation.
    """
    if len(payload) < 2:
        return "unknown"
    plain = TRANSFORMED_TO_PROFILE.get(bytes(payload[:2]))
    if plain is None:
        return "unknown"
    return PROFILE_NAMES[plain]


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class Transport:
    """Interface: open a device, then exchange frames."""

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def send_frame(self, frame: bytes) -> None:
        raise NotImplementedError

    def recv_frame(self, timeout: float) -> bytes:
        raise NotImplementedError

    def request(self, cmd: int, payload: bytes = b"", timeout: float = RECV_TIMEOUT):
        """Send a command and return (status, payload).  status 0 == success."""
        try:
            self.send_frame(build_frame(cmd, payload))
            rsp_cmd, status, rsp_payload = parse_response(self.recv_frame(timeout))
        except FlashError:
            raise
        except OSError as exc:
            # hidapi raises OSError when the device drops off the bus, which it
            # does whenever it re-enumerates (mode changes, resets).  Turn that
            # into a FlashError so callers can handle a disconnect instead of
            # crashing on a raw OSError.
            raise FlashError(
                f"device disconnected during 0x{cmd:02X} ({exc}) -- it most "
                "likely re-enumerated; reopen it and retry"
            ) from exc
        if rsp_cmd != cmd:
            raise FlashError(f"response command 0x{rsp_cmd:02X} != request 0x{cmd:02X}")
        return status, rsp_payload


class HidTransport(Transport):
    """hidapi-backed transport.  This is the real-hardware path.

    Writes go out as FEATURE reports, not output reports.  That is forced by the
    hardware, not a preference: the DK4's report descriptor declares report ID 1
    as Input-only (`Type 257`), so there is no output report to write to and
    `write()` fails with -1.  `send_feature_report` maps to
    `IOHIDDeviceSetReport(..., type 3, ...)` / `HidD_SetFeature`, which is what
    the vendor Mac tool (SetReport type 2) and Windows tool (SetFeature) both
    do.

    The device also echoes the received frame back on report ID 1 before
    answering, so `recv_frame` must skip any leading bytes that are not 0xED.
    """

    def __init__(self, vid: Optional[int], pid: Optional[int], usage=HID_USAGE):
        self.vid = vid
        self.pid = pid
        self.usage = usage
        self._dev = None

    def _enumerate(self):
        import hid  # lazily: importing this costs a dlopen, and `info` /
        # `selftest` never touch hardware.

        return [
            d
            for d in hid.enumerate(self.vid, self.pid)
            if d.get("usage_page") == HID_USAGE_PAGE and d.get("usage") == self.usage
        ]

    def open(self) -> None:
        import hid

        devices = self._enumerate()
        if not devices:
            if self.vid is None:
                raise FlashError("no matching HYISP HID interface found")
            raise FlashError(
                f"no matching HID interface for VID 0x{self.vid:04X} PID 0x{self.pid:04X}"
            )
        if len(devices) > 1:
            paths = "\n".join(f"  {d['path']}" for d in devices)
            raise FlashError(
                f"{len(devices)} matching interfaces found -- unplug the extras:\n{paths}"
            )
        dev = devices[0]
        try:
            self._dev = hid.device()
            self._dev.open_path(dev["path"])
        except OSError as exc:
            raise FlashError(f"cannot open {dev['path']}: {exc}") from exc
        # Non-blocking: a blocking read on macOS parks the process indefinitely
        # when the keyboard has nothing to say.  recv_frame owns the deadline.
        self._dev.set_nonblocking(1)

    def close(self) -> None:
        if self._dev is not None:
            self._dev.close()
            self._dev = None

    def send_frame(self, frame: bytes) -> None:
        n = (len(frame) + DATA_PER_REPORT - 1) // DATA_PER_REPORT
        for i in range(n):
            chunk = frame[i * DATA_PER_REPORT : (i + 1) * DATA_PER_REPORT]
            report = bytes((REPORT_ID,)) + chunk.ljust(DATA_PER_REPORT, b"\x00")
            # Feature report, not output report -- see the class docstring.
            written = self._dev.send_feature_report(report)
            if written != REPORT_SIZE:
                raise FlashError(f"short HID write: {written}/{REPORT_SIZE} bytes")

    def recv_frame(self, timeout: float) -> bytes:
        """Fetch one response frame.

        Verified against live DK4 hardware (VID 0x24F0 PID 0x0140): responses
        arrive on the FEATURE report channel, so this must poll
        `get_feature_report`, not `read()` -- `read()` only ever sees the
        keystroke reports on the other collection.

        `get_feature_report` returns the current feature-report buffer, which is
        zero-filled when the keyboard is idle, so buffers are scanned for the
        0xED magic and non-matching ones are discarded. A complete reply arrives
        in a single call, e.g. 0xB0:

            ed 0b b0 00 53 31 39 34 37 56 33 33 58
            ^magic  ^len  ^cmd ^st  ^------ "S1947V33" -----^  ^xor

        `len` = 11 = 1 (cmd) + 1 (status) + 8 (payload) + 1, checksum at index
        len+1, XOR over bytes 0..len -- matching build_frame()'s convention.
        """
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                raise FlashError(f"timed out after {timeout:g}s waiting for a response")
            buf = self._dev.get_feature_report(REPORT_ID, FEATURE_BUF_SIZE)
            if not buf:
                time.sleep(0.01)
                continue
            data = bytes(buf)
            start = data.find(bytes((RSP_MAGIC,)))
            if start < 0:
                continue  # idle / zero-filled buffer, or an echoed request
            if len(data) - start < 3:
                continue
            expected = data[start + 1] + 2
            frame = data[start : start + expected]
            if len(frame) < expected:
                continue  # not the whole frame yet; try again
            return frame


class MockTransport(Transport):
    """In-process fake device for --selftest.  No hardware required.

    Models a Nuvoton NUC123 running the HY ISP: it advertises its target size,
    stores raw (still-obfuscated) bytes exactly as they arrive, deobfuscates at
    reset time and validates the checksum -- which is what proves the host side
    wrote a correct image.
    """

    FLASH_KB = 32

    def __init__(self, fw: Firmware, expect_profile: bool = True, verbose: bool = True):
        self.fw = fw
        self.expect_profile = expect_profile
        self.verbose = verbose
        self.flash = bytearray(b"\xFF" * (self.FLASH_KB * 1024))
        self.written: set[tuple[int, int]] = set()
        self.stage = "application"
        self.erased = False
        self.protected = False
        self.reset = False
        self.model = 0x4215

    def open(self) -> None:
        self._log("device opened")

    def close(self) -> None:
        self._log("device closed")

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"    [dev] {msg}")

    def send_frame(self, frame: bytes) -> None:
        if frame[0] != REQ_MAGIC:
            raise FlashError(f"bad request magic 0x{frame[0]:02X}")
        if frame[-1] != _xor8(frame[:-1]):
            raise FlashError("request checksum mismatch")

    def recv_frame(self, timeout: float) -> bytes:
        return self._pending

    def _reply(self, cmd: int, status: int, payload: bytes = b"") -> None:
        length = 2 + len(payload) + 1
        body = bytes((RSP_MAGIC, length, cmd, status)) + payload
        self._pending = body + bytes((_xor8(body),))

    def request(self, cmd: int, payload: bytes = b"", timeout: float = RECV_TIMEOUT):
        if cmd in (CMD_ENTER_ISP, CMD_ENTER_LDROM_ISP):
            want = 4 if self.fw.is_isp else 32
            self.stage = "bootloader" if cmd == CMD_ENTER_LDROM_ISP else "isp"
            self._log(f"enter {'LDRom ISP' if cmd == CMD_ENTER_LDROM_ISP else 'ISP'}"
                      f" -> target {want} KB")
            self._reply(cmd, 0, bytes((want, self.model >> 8, self.model & 0xFF)))
            return 0, bytes((want, self.model >> 8, self.model & 0xFF))

        if cmd == CMD_CHECK_PROFILE:
            # Real B2175V13 behaviour: raw obfuscated bytes, minimum 8 bytes.
            # Anything else -- notably the Windows transform -- is refused 0x18,
            # and a refused profile makes the later 0xA4 erase fail too.
            want = self.fw.raw[-14:-4]
            ok = (
                self.expect_profile
                and len(payload) >= PROFILE_MIN_LEN
                and payload == want
            )
            self._log(f"profile check: got {payload.hex()} want {want.hex()} -> "
                      f"{'ok' if ok else 'MISMATCH'}")
            if ok:
                self._reply(cmd, 0, want[:2])
                return 0, want[:2]
            self._reply(cmd, PROFILE_CHECK_REJECTED, b"")
            return PROFILE_CHECK_REJECTED, b""

        if cmd == CMD_ERASE_CHIP:
            self.flash = bytearray(b"\xFF" * len(self.flash))
            self.written.clear()
            self.erased = True
            self._log("chip erased")
            self._reply(cmd, 0)
            return 0, b""

        if cmd == CMD_WRITE_FLASH:
            if not self.erased:
                self._reply(cmd, 0x0B)
                return 0x0B, b""
            addr = (payload[0] << 8) | payload[1]
            data = payload[2:]
            # Mirror the vendor tool's "empty" test: a block is skippable when it
            # matches the erased state, not when it is zero.  The sample images
            # pad with opposite bytes, so only the 0xFF rule is consistent.
            self.written.add((addr, len(data)))
            self.flash[addr : addr + len(data)] = data
            self._reply(cmd, 0)
            return 0, b""

        if cmd == CMD_PROTECT_CHIP:
            self.protected = True
            self._log("chip protected")
            self._reply(cmd, 0)
            return 0, b""

        if cmd == CMD_RESET:
            # The ISP deobfuscates and validates on reset (§5.4).  This is the
            # end-to-end proof that the whole write was correct.
            self.reset = True
            size = self.fw.size
            stored = bytes(self.flash[:size])
            wanted = self.fw.raw
            diffs = [i for i in range(size) if stored[i] != wanted[i]]
            if diffs:
                first = diffs[0]
                self._log(
                    f"RESET REFUSED: {len(diffs)} byte(s) differ, first at "
                    f"0x{first:04X} (flash 0x{stored[first]:02X} != "
                    f"file 0x{wanted[first]:02X})"
                )
                self._reply(cmd, 0x1B)
                return 0x1B, b""
            # Deobfuscate the WHOLE image, then split.  The checksum field is
            # itself stored obfuscated -- the device deobfuscates flash and only
            # then validates, so deobfuscating the body but reading the trailing
            # 4 bytes raw would not match.
            deob = deobfuscate(stored)
            ok = image_checksum(deob[:-4]) == struct.unpack("<I", deob[-4:])[0]
            self._log(f"reset: image verified byte-exact, checksum "
                      f"{'balances' if ok else 'FAILS'}")
            self._reply(cmd, 0 if ok else 0x1B)
            return (0 if ok else 0x1B), b""

        if cmd in (CMD_APP_VERSION, CMD_ISP_VERSION):
            self._reply(cmd, 0, b"\x00\x00")
            return 0, b"\x00\x00"

        self._reply(cmd, 0x01)
        return 0x01, b""


# ---------------------------------------------------------------------------
# The flashing sequence (§6)
# ---------------------------------------------------------------------------


@dataclass
class Progress:
    quiet: bool = False
    _last: str = field(default="", init=False)

    def step(self, msg: str) -> None:
        if not self.quiet:
            print(f"  {msg}")


def enter_isp(
    transport: Transport, fw: Firmware, expect_kb: Optional[int] = None
) -> int:
    """Step 6/7: enter ISP mode and verify the advertised target size.

    Returns the device's advertised flash size in KB.
    """
    if fw.is_isp:
        # Bootloader images cannot be flashed by this (ISP-mode) sequence: from
        # ISP mode 0xA1/0xA4 address APROM, so the 4 KB image would land in the
        # application window.  The bootloader-update route runs from
        # application mode instead -- see flash-bootloader.
        raise FlashError(
            f"{fw.path} is a 4 KB bootloader image and cannot be flashed from "
            "ISP mode: 0xA1/0xA4 address the application window there, so this "
            "sequence would overwrite the running application instead of the "
            "bootloader.\n"
            "Use `hyisp-util flash-bootloader` -- it writes the LDROM "
            "through the running application's flash path and verifies the "
            "new image before rebooting.\n"
            "Application images (L1947V33.bin / L2175V16.bin) use `flash` as "
            "usual."
        )
    cmd = CMD_ENTER_ISP
    label = "0xAA enter LDRom ISP (bootloader image)" if fw.is_isp else "0xA0 enter ISP"
    progress.step(f"{label}")
    status, payload = transport.request(cmd)
    if status != 0:
        raise FlashError(
            f"Enter ISP mode fail.\nPlease re-plug the keyboard (status 0x{status:02X})"
        )
    if len(payload) < 3:
        raise FlashError(f"0x{cmd:02X} returned {len(payload)} bytes, expected 3")
    kb = payload[0]
    model = (payload[1] << 8) | payload[2]
    progress.step(f"device reports target {kb} KB, model 0x{model:04X}")

    # The guard the vendor tools apply: the file must be exactly the advertised
    # size.  This is a per-mode target size, NOT a capacity check (§6.4).
    if fw.size != kb * 1024:
        raise FlashError(
            f"Firmware file size is not correct.\n"
            f"device expects {kb} KB ({kb * 1024} bytes), file is {fw.size} bytes"
        )
    if expect_kb is not None and kb != expect_kb:
        raise FlashError(f"device reports {kb} KB, expected {expect_kb} KB")
    return kb


def check_profile(transport: Transport, fw: Firmware) -> str:
    """Step 9: 0xA5 with the profile block taken **raw** from the file.

    Measured on a real DK4 (bootloader B2175V13), this is the payload format the
    device actually accepts, and getting it wrong is not cosmetic:

    - Sending the **raw obfuscated** profile bytes returns `status 0x00`.
    - Sending the Windows-tool **transformed** bytes (§5.3) returns `0x18` for
      every length tried.
    - Payloads shorter than 8 bytes return `0x18` regardless of encoding, so the
      device enforces a minimum length before it compares anything.

    `0x18` from `0xA5` is not merely "unsupported": the subsequent `0xA4` erase
    also fails with `0x18` when the profile was not accepted, so a bad profile
    payload locks out the whole update.  This inverts the usual reading of the
    "macOS bug" -- on this bootloader the raw send that looked like the macOS
    tool's bug is the correct behaviour, and the Windows transform is the one
    that fails.
    """
    progress.step("0xA5 check profile")
    payload = fw.raw[-14:-4]
    if len(payload) < PROFILE_MIN_LEN:
        raise FlashError(
            f"profile block is {len(payload)} bytes, device needs at least "
            f"{PROFILE_MIN_LEN}"
        )
    status, rsp = transport.request(CMD_CHECK_PROFILE, payload)
    if status == PROFILE_CHECK_REJECTED:
        raise FlashError(
            f"Checking Profile fail (status 0x{status:02X}) -- the device refused "
            "the profile, and 0xA4 erase will fail with the same status until "
            "the profile is accepted"
        )
    if status != 0:
        raise FlashError(f"Checking Profile fail (status 0x{status:02X})")
    name = profile_name_from_response(rsp)
    progress.step(f"profile accepted: {name}")
    return name


def write_image(
    transport: Transport, fw: Firmware, chunk: int = DEFAULT_CHUNK, dry_run: bool = False
) -> None:
    """Steps 10-12: erase, then two-pass write with retries.

    Pass 1 walks 256-byte blocks from 0x100 to the end, skipping blocks that match
    the erased state.  Pass 2 rewrites the first `VECTOR_TABLE_BYTES`, so the
    vector table -- the region the MCU reads to decide where to start executing
    -- is committed last.
    """
    if dry_run:
        progress.step("0xA4 erase chip (dry run, skipped)")
        return

    progress.step("0xA4 erase chip")
    status, _ = transport.request(CMD_ERASE_CHIP)
    if status != 0:
        raise FlashError(f"Chip Erase fail (status 0x{status:02X})")

    def write_at(addr: int, data: bytes) -> None:
        for off in range(0, len(data), chunk):
            piece = data[off : off + chunk]
            for attempt in range(1, RETRIES_PER_CHUNK + 1):
                try:
                    status, _ = transport.request(
                        CMD_WRITE_FLASH,
                        bytes((addr >> 8, addr & 0xFF)) + piece,
                    )
                    if status == 0:
                        break
                    last = status
                except FlashError as exc:
                    last = str(exc)
                if attempt == RETRIES_PER_CHUNK:
                    raise FlashError(
                        f"Retried {RETRIES_PER_CHUNK} times, "
                        f"Update fail at 0x{addr >> 8:02X}{addr & 0xFF:02X}: {last}"
                    ) from None
            addr += len(piece)

    # Pass 1: body, skipping blocks that already match the erased state, from
    # 0x100 to end of image.
    #
    # "Empty" means all-0xFF (erased), NOT all-zero.  This distinction matters:
    # the two sample images pad with opposite bytes -- the bootloader leaves one
    # all-0xFF block, the applications leave 46 all-0x00 blocks -- so a
    # skip-all-zero test would silently skip application padding and corrupt it.
    # Skipping against the erased value is the only rule consistent with both,
    # and it is what keeps the deobfuscated flash byte-identical to the
    # deobfuscated file (and so keeps the §5.4 checksum balancing).
    skipped = 0
    blocks = 0
    for start in range(0x100, fw.size, BLOCK_SIZE):
        block = fw.raw[start : start + BLOCK_SIZE]
        if all(b == 0xFF for b in block):
            skipped += 1
            continue
        write_at(start, block)
        blocks += 1
    progress.step(
        f"pass 1: wrote {blocks} block(s) from 0x0100, skipped {skipped} empty"
    )

    # Pass 2: vector table region last.
    head = min(VECTOR_TABLE_BYTES, fw.size)
    write_at(0, fw.raw[:head])
    progress.step(f"pass 2: wrote 0x0000-0x{head - 1:04X} (vector table last)")


def protect_and_reset(transport: Transport, fw: Firmware, dry_run: bool = False) -> None:
    """Steps 13-14."""
    if dry_run:
        progress.step("0xA8 protect + 0xAF reset (dry run, skipped)")
        return
    progress.step("0xA8 protect chip")
    status, _ = transport.request(CMD_PROTECT_CHIP)
    if status != 0:
        raise FlashError(f"Protect fail (status 0x{status:02X})")

    progress.step("0xAF reset")
    status, _ = transport.request(CMD_RESET)
    if status != 0:
        # The vendor tool distinguishes 0x1B as a bootloader-specific failure.
        if status == 0x1B:
            raise FlashError("Bootloader upgrade fail, please retry.")
        raise FlashError(f"Reset fail (status 0x{status:02X})")


def flash(
    transport: Transport, fw: Firmware, chunk: int = DEFAULT_CHUNK, dry_run: bool = False
) -> None:
    """The complete sequence, minus the USB re-enumeration pause."""
    enter_isp(transport, fw)
    check_profile(transport, fw)
    write_image(transport, fw, chunk=chunk, dry_run=dry_run)
    protect_and_reset(transport, fw, dry_run=dry_run)


def open_bootloader_image(path: str) -> tuple[bytes, str]:
    """Load a 4 KB bootloader image and return its **wire form** plus a note.

    The device's `0xA1` handler deobfuscates every payload byte before
    programming, so the bytes sent must be the obfuscated form the vendor
    images already use.  Two input forms are accepted:

    - a vendor-container image (`A2175V13.bin` and clones) -- sent verbatim;
    - a plaintext dump read back from flash (e.g. via the DFU route) -- the
      HYISP signature then appears in the clear at 0xFF4 and the image is
      obfuscated in memory before sending.

    Both must satisfy the device's own `0xAF` verify gate (HYISP signature at
    0xFF4 and a zero uint32 word sum over the whole 4 KB) or the update would
    be written and then rejected, leaving the LDROM erased; check that here,
    before anything is touched.
    """
    raw = read_image(path)
    if len(raw) != ISP_IMAGE_SIZE:
        hint = ""
        if len(raw) == APPLICATION_IMAGE_SIZE:
            hint = "  That is an application image -- use `flash` for it."
        raise FlashError(
            f"{path} is {len(raw)} bytes; a bootloader image is exactly "
            f"{ISP_IMAGE_SIZE} ({ISP_IMAGE_SIZE // 1024} KB).{hint}"
        )

    def word_sum_ok(image: bytes) -> bool:
        total = sum(struct.unpack("<%dI" % (ISP_IMAGE_SIZE // 4), image)) & 0xFFFFFFFF
        return total == 0

    container_err: Optional[FlashError] = None
    try:
        fw = _parse_vendor_image(path, raw)
        if not fw.is_isp:
            raise FlashError(
                f"{path} is an application image; use `flash` for it, or pass a "
                "4 KB bootloader image to flash-bootloader"
            )
        if not fw.checksum_ok:
            raise FlashError(
                f"{path} has an unbalanced checksum (stored 0x{fw.stored_checksum:08X}, "
                f"calculated 0x{fw.calculated_checksum:08X}); the device's 0xAF "
                "verify would reject it after writing (status 0x1B)"
            )
        return raw, "vendor container, sent verbatim"
    except FlashError as exc:
        container_err = exc

    # Not a vendor container: maybe a plaintext dump with HYISP in the clear.
    if raw[0xFF4:0xFF9] == b"HYISP":
        if not word_sum_ok(raw):
            raise FlashError(
                f"{path} looks like a plaintext bootloader dump but its uint32 "
                "word sum is not zero; the device's 0xAF verify would reject it "
                "after writing (status 0x1B)"
            )
        return obfuscate(raw), "plaintext dump, obfuscated in memory"
    raise FlashError(
        f"{path} is neither a vendor bootloader container nor a plaintext dump "
        f"(no HYISP signature at 0xFF4 in either encoding)\n{container_err}"
    )


def flash_bootloader(
    transport: Transport, wire: bytes, chunk: int = BOOTLOADER_WRITE_CHUNK
) -> None:
    """Application-side LDROM update: auth, erase, program, latch, verify.

    The running application implements a second copy of the flash commands
    whose addresses target the 4 KB LDROM window at 0x100000.  Three details
    differ from the ISP-mode sequence and all three are load-bearing:

    - `0xA5` takes plaintext ``GF`` (the application's profile pool is the
      ISP handshake string, not the firmware trailer the bootloader checks);
    - `0xA1` carries a big-endian 16-bit offset and deobfuscates the data, so
      the vendor-container bytes go on the wire as they appear in the file;
    - `0xAA` must latch before `0xAF`, which then verifies the HYISP
      signature and the 4 KB checksum and only then resets.  On 0x1B the
      device stays in application mode, so the write can simply be retried.
    """
    progress.step("0xA5 auth (application side, plaintext GF)")
    status, _ = transport.request(CMD_CHECK_PROFILE, APP_AUTH_PAYLOAD)
    if status == PROFILE_CHECK_REJECTED:
        raise FlashError(
            "0xA5 auth refused (status 0x18) -- the running application does not "
            "accept the application-side flash path.  It must be a vendor "
            "application image (e.g. L1947V33 / L2175V16); third-party firmware "
            "without this path cannot update the bootloader."
        )
    if status != 0:
        raise FlashError(f"0xA5 auth failed (status 0x{status:02X})")

    progress.step("0xA4 erase LDROM")
    status, _ = transport.request(CMD_ERASE_CHIP)
    if status != 0:
        raise FlashError(f"0xA4 erase failed (status 0x{status:02X})")

    for off in range(0, ISP_IMAGE_SIZE, chunk):
        piece = wire[off : off + chunk]
        status, _ = transport.request(
            CMD_WRITE_FLASH, bytes((off >> 8, off & 0xFF)) + piece
        )
        if status != 0:
            raise FlashError(
                f"0xA1 write failed at 0x{off:03X} (status 0x{status:02X}); the "
                "LDROM is now incomplete -- re-run flash-bootloader to rewrite it "
                "(the application is untouched and still running)"
            )
        progress.step(f"0xA1 wrote 0x{off:03X}-0x{off + len(piece) - 1:03X}")
        time.sleep(0.005)

    progress.step("0xAA latch (0xAF will now verify before resetting)")
    status, _ = transport.request(CMD_ENTER_LDROM_ISP)
    if status != 0:
        raise FlashError(f"0xAA latch failed (status 0x{status:02X})")

    progress.step("0xAF verify + reset")
    status, _ = transport.request(CMD_RESET)
    if status == 0x1B:
        raise FlashError(
            "device rejected the new bootloader image (status 0x1B): the HYISP "
            "signature or the 4 KB checksum did not match.  No reset happened -- "
            "the keyboard is still running the application, with the LDROM "
            "erased.  Re-run flash-bootloader with a known-good image."
        )
    if status != 0:
        raise FlashError(f"0xAF failed (status 0x{status:02X})")


progress = Progress()


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------


def find_device(want_isp: bool) -> dict:
    import hid

    candidates = []
    seen_paths = set()
    for info in hid.enumerate():
        if info.get("vendor_id") not in KNOWN_VENDORS:
            continue
        if info.get("usage_page") != HID_USAGE_PAGE:
            continue
        if info.get("usage") != HID_USAGE:
            continue
        # One keyboard exposes several HID collections on the same path (vendor,
        # consumer control, boot keyboard).  hidapi lists them separately, so
        # dedupe by path or the flasher will see a phantom "multiple keyboards".
        if info["path"] in seen_paths:
            continue
        seen_paths.add(info["path"])
        candidates.append(info)

    if not candidates:
        raise FlashError(
            "no HYISP device interface found.\n"
            "The keyboard must be plugged in and NOT already in ISP mode."
        )
    if len(candidates) > 1:
        listing = "\n".join(
            f"  0x{c['vendor_id']:04X}:0x{c['product_id']:04X}  {c['product_string'] or ''}"
            for c in candidates
        )
        raise FlashError(f"multiple keyboards found -- unplug all but one:\n{listing}")

    dev = candidates[0]
    in_isp = dev["product_id"] in ISP_MODE_PIDS
    if want_isp and not in_isp:
        raise FlashError(
            "device is not in ISP mode.  Re-run --flash; it re-enumerates "
            "automatically after the enter-ISP command."
        )
    if not want_isp and in_isp:
        # A reset (0xAF) makes the device re-enumerate, and the stale ISP entry
        # can still be listed for a moment after the application one appears.
        # Poll briefly before declaring it stuck in ISP mode, so a keyboard
        # that is merely mid-transition is not misdiagnosed.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            time.sleep(0.25)
            for info in hid.enumerate():
                if info.get("vendor_id") not in KNOWN_VENDORS:
                    continue
                if info.get("product_id") in ISP_MODE_PIDS:
                    continue
                if info["path"] in seen_paths:
                    continue
                if info.get("usage_page") == HID_USAGE_PAGE and info.get("usage") == HID_USAGE:
                    return info
        raise FlashError(
            "device is already in ISP mode; it cannot accept an application "
            "update.  Unplug it, then plug it back in to leave ISP mode."
        )
    return dev


def find_device_for_flash() -> tuple[dict, bool]:
    """Locate the keyboard for a flash, accepting either mode.  Returns
    (device, already_in_isp).

    Normally `flash` starts from application mode and enters ISP mode itself with
    `0xA0`.  But a keyboard can also be *left* in ISP mode -- by the user, or by
    third-party firmware that offers a restart-into-bootloader path and exposes no
    application-mode HID endpoints to talk to beforehand.  Everything after the
    entry command is identical either way, because the bootloader is the part that
    does the writing, so accept that case instead of refusing it: refusing would
    strand exactly the units that most need the tool.
    """
    try:
        return find_device(want_isp=False), False
    except FlashError:
        return find_device(want_isp=True), True


def wait_for_isp_device(timeout: float = 15.0) -> dict:
    """Poll until the keyboard re-enumerates as an 'ISP Mode' device.

    After 0xA0/0xAA the device drops off the bus and comes back on the vendor's
    ISP-only PID (0x0000, or 0x0001 for the macOS-package bootloader image).
    The vendor tool blocks on this; we poll with a deadline.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            dev = find_device(want_isp=True)
            time.sleep(0.5)  # let the device settle after re-enumeration
            return dev
        except FlashError:
            time.sleep(0.25)

    # Distinguish "never entered ISP mode" from "entered it and vanished": if
    # the keyboard is still sitting in application mode it rejected the entry
    # outright.  0xAA does exactly this on real hardware -- it answers 0x00 but
    # leaves the device in application mode, so the generic "unplug it" advice
    # below would be wrong and unhelpful.
    try:
        find_device(want_isp=False)
    except FlashError:
        pass
    else:
        raise FlashError(
            f"the keyboard is still in application mode after {timeout:g}s -- it did "
            "not enter ISP mode.\n"
            "0xAA (LDRom ISP) answers status 0x00 on this hardware but does not switch "
            "modes, so it cannot be used to reach ISP mode the way 0xA0 does."
        )
    raise FlashError(
        f"device did not re-enumerate as 'ISP Mode' within {timeout:g}s.\n"
        "Unplug and replug the keyboard, then re-run."
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_list(_args) -> int:
    import hid

    rows = []
    seen_paths = set()
    for info in hid.enumerate():
        if info.get("vendor_id") not in KNOWN_VENDORS:
            continue
        vid, pid = info["vendor_id"], info["product_id"]
        name = info.get("product_string") or ""
        state = "ISP MODE" if pid in ISP_MODE_PIDS else "application"
        page, usage = info.get("usage_page", 0), info.get("usage", 0)
        vendor_iface = page == HID_USAGE_PAGE and usage == HID_USAGE
        role = "vendor (flashing)" if vendor_iface else "other collection"
        # hidapi lists each HID collection separately; the keyboard exposes
        # three (vendor, consumer control, boot keyboard) on one physical path.
        dupe = "" if info["path"] not in seen_paths else "  [same device]"
        seen_paths.add(info["path"])
        rows.append((vid, pid, state, name, page, usage, role, info["path"], dupe))

    if not rows:
        print("no HYISP device interfaces found")
        return 1
    devices = len({r[7] for r in rows})
    for vid, pid, state, name, page, usage, role, path, dupe in rows:
        print(f"0x{vid:04X}:0x{pid:04X}  {state:12s} {name}")
        print(f"    page 0x{page:02X} usage 0x{usage:02X}  {role}")
        print(f"    path {path.decode() if isinstance(path, bytes) else path}{dupe}")
    print(f"\n{devices} device(s), {len(rows)} HID collection(s)")
    return 0


def cmd_info(args) -> int:
    fw = load_firmware(args.firmware)
    print(f"file           {fw.path}")
    print(f"size           {fw.size} bytes ({fw.size // 1024} KB)")
    print(f"image type     {fw.kind}")
    print(f"magic          {fw.magic!r} (Heng Yu)")
    print(f"profile        {fw.profile!r}")
    print(f"  region       {fw.profile[:2]!r} = {fw.region}")
    print(f"  identifier   {fw.identifier.decode('ascii', 'replace')!r}")
    print(f"  type flag    0x{fw.type_flag:02X} "
          f"({'bootloader' if fw.type_flag == 0x55 else 'application'})")
    print(f"checksum       stored 0x{fw.stored_checksum:08X}  "
          f"calculated 0x{fw.calculated_checksum:08X}  "
          f"{'OK' if fw.checksum_ok else 'MISMATCH'}")
    if fw.is_isp:
        print("entry command  n/a -- use `flash-bootloader` to update the "
              "bootloader from application mode")
    else:
        print(f"entry command  0x{CMD_ENTER_ISP:02X}")
    print(f"profile frame  {build_frame(CMD_CHECK_PROFILE, fw.raw[-14:-4]).hex(' ')}")
    version = image_version_string(fw)
    variant = KNOWN_VARIANTS.get(version)
    if variant:
        vpid, target_os = variant
        print(f"version        {version!r}")
        print(f"  variant      {target_os} firmware, enumerates as 0x24F0:{vpid:04X}")
    else:
        print(f"version        {version!r}  (variant not in KNOWN_VARIANTS)")
    if not fw.checksum_ok:
        print("\nWARNING: checksum does not balance.  The device will refuse to boot "
              "this image (§5.4).", file=sys.stderr)
        return 1
    return 0


def open_firmware(path: str, convert: bool = False, kb: int = 32) -> Firmware:
    """Load `path` for use by flash/verify/selftest, wrapping it if asked.

    `--convert` is what lets a bare open-source build be used without a separate
    conversion step: the wrapper runs on the bytes in memory, so nothing on disk
    changes and every later stage sees an ordinary vendor image.
    """
    if convert:
        return wrap_bare_image(path, kb * 1024)
    return load_firmware(path)


def cmd_convert(args) -> int:
    """Wrap a bare firmware image in the vendor container and write it to disk."""
    fw = wrap_bare_image(args.firmware, args.kb * 1024, force=args.force)
    dest = args.output or (os.path.splitext(args.firmware)[0] + ".vendor.bin")
    if os.path.exists(dest) and not args.force:
        raise FlashError(f"{dest} already exists; pass -o to choose another name")

    padded = fw.size - TRAILER_SIZE - os.path.getsize(args.firmware)
    with open(dest, "wb") as fh:
        fh.write(fw.raw)

    print(f"source         {args.firmware}  "
          f"({os.path.getsize(args.firmware)} bytes, bare image)")
    print(f"output         {dest}  ({fw.size} bytes, {args.kb} KB)")
    print(f"  padded       {padded} byte(s) of 0xFF")
    print(f"  trailer      {APPLICATION_TRAILER_PREFIX.decode('ascii', 'replace')!r}"
          f" + checksum, obfuscated with the rest of the image (§5.0)")
    print(f"  magic        {fw.magic!r}")
    print(f"  profile      {fw.identifier.decode('ascii', 'replace')!r}"
          f" ({fw.region}), type flag 0x{fw.type_flag:02X}")
    print(f"  checksum     0x{fw.stored_checksum:08X}  "
          f"{'balances' if fw.checksum_ok else 'DOES NOT BALANCE'}")

    if not fw.checksum_ok:
        print(f"\nerror: built image does not validate", file=sys.stderr)
        return 1

    # Reload from disk so what follows proves the *file* is flashable, not just
    # the bytes we happened to hold in memory.
    reloaded = load_firmware(dest)
    if reloaded.raw != fw.raw:
        print(f"\nerror: {dest} does not read back identically", file=sys.stderr)
        return 1

    dev = MockTransport(reloaded, expect_profile=True)
    dev.verbose = args.verbose
    flash(dev, reloaded)
    print("\n  mock device: written byte-exact, checksum validated by the device")
    print(f"\nFlash it with:\n  hyisp-util verify {dest}\n"
          f"  hyisp-util flash {dest} --yes")
    return 0


def cmd_selftest(args) -> int:
    """Run the whole sequence against the in-process mock device."""
    fw = open_firmware(args.firmware, args.convert, args.kb)
    print(f"selftest: {describe_image(fw)}")
    if not fw.checksum_ok:
        print("  refusing: checksum does not balance", file=sys.stderr)
        return 1

    original = check_profile
    if args.windows_profile:
        # The Windows-tool transform is what this bootloader REJECTS, so
        # "reproduce the Windows behaviour" now means sending transformed bytes
        # and watching the profile check -- and then the erase -- fail with
        # 0x18.  Raw is the correct encoding.
        def check_profile_transformed(transport, fw_):
            progress.step("0xA5 check profile (Windows-transformed, as Windows does)")
            status, _ = transport.request(
                CMD_CHECK_PROFILE, build_profile_payload(fw_.raw[-14:-4])
            )
            if status != 0:
                raise FlashError(f"Checking Profile fail (status 0x{status:02X})")
            return original(transport, fw_)

        globals()["check_profile"] = check_profile_transformed
        print("  (Windows tool behaviour: sending transformed profile bytes)")

    try:
        dev = MockTransport(fw, expect_profile=True)
        flash(dev, fw)
    except FlashError as exc:
        if fw.is_isp:
            # flash() refuses 4 KB bootloader images up front: from ISP mode
            # 0xA1/0xA4 address the application window, so there is nothing to
            # mock-test on that route.  Bootloader updates go through
            # flash-bootloader's application-side sequence, which this mock
            # does not model; it is validated on hardware.
            print("  SKIPPED: `flash` refuses bootloader images -- bootloader "
                  "updates use flash-bootloader (application-side route)")
            return 0
        print(f"  EXPECTED FAILURE: {exc}")
        return 0
    finally:
        globals()["check_profile"] = original
    assert dev.reset and dev.protected
    print("  PASS: image written byte-exact, checksum validated by the device")
    return 0


def warn_on_variant_mismatch(fw: Firmware, dev: dict) -> None:
    """Flag a Windows/macOS variant change, which 0xA5 will not catch.

    Both variants carry the identical GFD4215 profile, so the profile check
    accepts either and the update succeeds -- the only symptom is misplaced
    keys afterwards.  The application PID is the reliable discriminator, so
    compare the running device against the image's known variant.
    """
    target = KNOWN_VARIANTS.get(image_version_string(fw))
    running_pid = dev["product_id"]
    if target is None or fw.is_isp:
        return
    # PID 0 is the ISP sentinel, not an application variant: a keyboard sitting
    # in bootloader mode is running no application at all, so there is nothing
    # to disagree with and the comparison would only report noise.
    if running_pid in ISP_MODE_PIDS:
        return
    target_pid, target_os = target
    running = next(
        (os_name for pid, os_name in KNOWN_VARIANTS.values() if pid == running_pid),
        None,
    )
    if running is None or running_pid == target_pid:
        return
    print(
        f"\n  NOTE: this unit is running {running} firmware "
        f"(0x{running_pid:04X}); {fw.path} is {target_os} firmware "
        f"(0x{target_pid:04X}).\n"
        f"  0xA5 accepts both -- same GFD4215 profile -- so this will succeed "
        f"and change the key layout.\n"
        f"  Unit will re-enumerate as 0x24F0:{target_pid:04X} after the reset.\n",
        file=sys.stderr,
    )


def cmd_flash(args) -> int:
    """Erase, write and verify one image, entering ISP mode if not already in it."""
    fw = open_firmware(args.firmware, args.convert, args.kb)
    print(describe_image(fw))
    if fw.source_path:
        # The bytes about to be written are not the bytes on disk, so say so
        # plainly before anything is erased.
        padded = fw.size - TRAILER_SIZE - os.path.getsize(fw.source_path)
        print(f"  wrapping the bare image in the vendor container in memory: "
              f"{padded} byte(s) of 0xFF padding, trailer, checksum, obfuscation")
        print("  nothing is written to disk; use `convert` to keep the wrapped "
              "image as a file")
    if not fw.checksum_ok:
        print(
            f"checksum does not balance (stored 0x{fw.stored_checksum:08X}, "
            f"calculated 0x{fw.calculated_checksum:08X}).\n"
            "The device will refuse to boot this image.  Aborting.",
            file=sys.stderr,
        )
        return 1
    if args.dry_run:
        print("DRY RUN -- no bytes will be written")
    elif not args.yes:
        print(
            f"\nAbout to erase and reflash {fw.kind} on a HYISP device.\n"
            f"Unplug the keyboard first, then press Enter, or pass --yes.",
            file=sys.stderr,
        )
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            print("\naborted", file=sys.stderr)
            return 1

    dev, already_in_isp = find_device_for_flash()
    print(f"found 0x{dev['vendor_id']:04X}:0x{dev['product_id']:04X} "
          f"{dev.get('product_string') or ''}")

    warn_on_variant_mismatch(fw, dev)

    if already_in_isp:
        isp = dev
        print("  already in ISP mode; skipping the 0xA0 entry step")
        print(f"  target size not confirmed (no 0xA0 reply); the device validates "
              f"the image checksum itself on reset")
    else:
        t = HidTransport(dev["vendor_id"], dev["product_id"])
        t.open()
        try:
            enter_isp(t, fw)
            print("  waiting for the keyboard to re-enumerate in ISP mode...")
        finally:
            t.close()

        isp = wait_for_isp_device()
        print(f"  re-enumerated: 0x{isp['vendor_id']:04X}:0x{isp['product_id']:04X} "
              f"{isp.get('product_string') or 'ISP Mode'}")

    t2 = HidTransport(isp["vendor_id"], isp["product_id"])
    t2.open()
    try:
        check_profile(t2, fw)
        write_image(t2, fw, chunk=args.chunk, dry_run=args.dry_run)
        protect_and_reset(t2, fw, dry_run=args.dry_run)
    finally:
        t2.close()

    if args.dry_run:
        print("\nDRY RUN complete -- nothing was written")
    else:
        print("\nUpdate successfully.")
    return 0


def cmd_flash_bootloader(args) -> int:
    """Update the 4 KB LDROM bootloader through the application-side flash path.

    Unlike `flash`, this never enters ISP mode to do the writing: the running
    application is the writer, its 0xA1/0xA4 target the LDROM window, and its
    latched 0xAF verifies the new image (HYISP signature + 4 KB checksum)
    before resetting.  The keyboard therefore must start in -- and ends in --
    application mode; entering the new bootloader (0xA0) is done afterwards
    only to prove it boots.
    """
    wire, note = open_bootloader_image(args.firmware)
    version = image_version_string(
        Firmware(
            path=args.firmware, raw=wire, is_isp=True, profile=b"", magic=b"",
            identifier=b"HYISP", type_flag=0x55, stored_checksum=0,
            calculated_checksum=0,
        )
    )
    print(
        f"{args.firmware}: {ISP_IMAGE_SIZE} bytes, bootloader (ISP), {note}"
        + (f", version {version!r}" if version != "<unknown>" else "")
    )
    if args.dry_run:
        print(
            "DRY RUN -- the following sequence would run against an "
            "application-mode keyboard:\n"
            "  0xA5 auth (plaintext GF), 0xA4 erase LDROM,\n"
            f"  0xA1 x {-(-ISP_IMAGE_SIZE // args.chunk)} chunk(s) of up to "
            f"{args.chunk} bytes, 0xAA latch, 0xAF verify+reset,\n"
            "  0xA0 to enter the new bootloader, 0xAF to return to application mode"
        )
        return 0
    if not args.yes:
        print(
            "\nAbout to erase and replace the bootloader (LDROM) of a HYISP "
            "device.\nThe application keeps running and the device verifies the "
            "new image\n(HYISP signature + checksum) before it reboots; a failed "
            "verify leaves\nthe keyboard in application mode and the write can be "
            "retried.\n\nPress Enter, or pass --yes.",
            file=sys.stderr,
        )
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            print("\naborted", file=sys.stderr)
            return 1

    try:
        dev = find_device(want_isp=False)
    except FlashError as exc:
        raise FlashError(
            f"{exc}\nflash-bootloader writes through the running application, so "
            "the keyboard must be in application mode.  If it is stuck in ISP "
            "mode, run `hyisp-util reset` first (or unplug and replug)."
        ) from None
    print(f"found 0x{dev['vendor_id']:04X}:0x{dev['product_id']:04X} "
          f"{dev.get('product_string') or ''} (application mode)")

    t = HidTransport(dev["vendor_id"], dev["product_id"])
    t.open()
    try:
        flash_bootloader(t, wire, chunk=args.chunk)
    finally:
        t.close()

    print("  waiting for the keyboard to come back in application mode...")
    app = wait_for_device(want_isp=False, timeout=args.timeout)
    print(f"  application running again: 0x{app['vendor_id']:04X}:"
          f"0x{app['product_id']:04X} {app.get('product_string') or ''}")

    # Enter the new bootloader to prove it boots.  The 0xA0 reply can be lost
    # if the device resets while the read is in flight -- it re-enumerates as
    # ISP either way -- so poll for the ISP device first and only re-send 0xA0
    # when the previous attempt demonstrably did not take.
    print("  0xA0: entering the new bootloader to confirm it boots")
    isp = None
    last_err: Optional[FlashError] = None
    for _attempt in range(3):
        try:
            isp = wait_for_device(want_isp=True, timeout=3.0)
            break
        except FlashError:
            pass
        try:
            app = wait_for_device(want_isp=False, timeout=10.0)
        except FlashError as exc:
            last_err = exc
            continue
        time.sleep(0.5)  # let the freshly re-enumerated application settle
        t2 = HidTransport(app["vendor_id"], app["product_id"])
        t2.open()
        try:
            try:
                status, payload = t2.request(CMD_ENTER_ISP)
            except FlashError as exc:
                # Most likely a disconnect caused by the device accepting the
                # command and re-enumerating; the next iteration polls ISP mode.
                last_err = FlashError(f"0xA0 reply lost ({exc})")
                print(f"  ({last_err}; polling for ISP mode)")
                continue
            if status != 0:
                last_err = FlashError(
                    f"0xA0 failed after the update (status 0x{status:02X}); the "
                    "new bootloader image did not boot.  The application is "
                    "still running."
                )
                print(f"  ({last_err})")
                continue
            # payload[0] is the APROM size in KB (32 on the NUC123), not the
            # LDROM size; only log it.  Success is proven by the ISP
            # re-enumeration and the 0xA6 reply below.
            if payload:
                print(f"  (0xA0 replied, APROM size {payload[0]} KB)")
        finally:
            t2.close()
    if isp is None:
        raise last_err or FlashError(
            "the keyboard did not enter ISP mode after the bootloader update"
        )

    print(f"  new bootloader live: 0x{isp['vendor_id']:04X}:0x{isp['product_id']:04X} "
          f"{isp.get('product_string') or 'ISP Mode'}")
    t3 = HidTransport(isp["vendor_id"], isp["product_id"])
    t3.open()
    try:
        status, payload = t3.request(CMD_ISP_VERSION)
    finally:
        t3.close()
    if status == 0 and payload:
        print(f"  0xA6 ISP version: {payload.decode('ascii', 'replace')!r}")
    else:
        print(f"  0xA6 ISP version: no reply (status 0x{status:02X})")
    pid_note = ("(original/Windows-package PID)" if isp["product_id"] == ISP_MODE_PID
                else "(macOS-package PID)")
    print(f"  note: PID 0x{isp['product_id']:04X} {pid_note}")

    if args.stay_in_isp:
        print("\nUpdate successfully; keyboard left in ISP mode "
              "(run `hyisp-util reset` to return it to application mode).")
    else:
        reset_to_app_mode(timeout=args.timeout)
        print("\nUpdate successfully; keyboard returned to application mode.")
    return 0


def wait_for_device(want_isp: bool, timeout: float = 15.0) -> dict:
    """find_device with a deadline, tolerating transient absence.

    The DK4 genuinely vanishes from HID enumeration for a while across a USB
    re-enumeration -- observed up to ~18 s on a replug -- so an immediate
    find_device after any mode change is not reliable.
    """
    deadline = time.monotonic() + timeout
    last = None
    while True:
        try:
            return find_device(want_isp=want_isp)
        except FlashError as exc:
            last = exc
            if time.monotonic() >= deadline:
                raise last from None
            time.sleep(0.25)


def reset_to_app_mode(timeout: float = 20.0) -> dict:
    """Send 0xAF and wait for the keyboard to come back as an application device.

    Works from either mode and is the software equivalent of an unplug/replug.
    This matters because 0xAA latches the device into a state where 0xA0 no
    longer enters ISP mode, and a plain reset clears it (verified on two DK4
    units) -- so recovery does not need physical access, despite the vendor
    tool telling users to unplug.
    """
    try:
        dev = find_device(want_isp=True)
    except FlashError:
        dev = find_device(want_isp=False)

    t = HidTransport(dev["vendor_id"], dev["product_id"])
    t.open()
    try:
        status, _ = t.request(CMD_RESET, timeout=5.0)
    finally:
        t.close()
    if status != 0:
        raise FlashError(f"0xAF reset failed (status 0x{status:02X})")

    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            dev = find_device(want_isp=False)
        except FlashError as exc:
            last = exc
            time.sleep(0.25)
            continue
        # Enumerating is not the same as usable: straight after a reset the
        # entry can still be stale and the handle dies on first use.  Only
        # return once the device actually answers.
        t = HidTransport(dev["vendor_id"], dev["product_id"])
        try:
            t.open()
            status, _ = t.request(CMD_APP_VERSION, timeout=3.0)
            if status == 0:
                return dev
            last = FlashError(f"0xB0 answered status 0x{status:02X}")
        except (FlashError, OSError) as exc:
            last = exc
        finally:
            t.close()
        time.sleep(0.5)
    raise FlashError(
        f"keyboard did not come back in application mode within {timeout:g}s"
        + (f" ({last})" if last else "")
        + ".\nUnplug and replug it, then re-run."
    )


def cmd_verify(args) -> int:
    """Full read-only conformance check across the whole protocol surface.

    Writes nothing: no erase, no flash write, no protect.  It does move the
    keyboard between application and ISP mode, and (unless --no-latch) it
    deliberately triggers the 0xAA latch to prove the tool can recover from it,
    which is the only way to find out that recovery actually works.

    Every check records an expected/actual pair so two units can be diffed.
    """
    checks: list[tuple[str, object, object, bool]] = []

    def check(name, expected, actual, ok=None):
        if ok is None:
            ok = expected == actual
        checks.append((name, expected, actual, ok))
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: expected {expected!r}, got {actual!r}")

    def summarize(app_version: str, isp_version: Optional[str] = None) -> int:
        failed = [c for c in checks if not c[3]]
        print(f"\n{'PASS' if not failed else 'FAIL'}: {len(checks) - len(failed)}"
              f"/{len(checks)} checks passed")
        if failed:
            for name, expected, actual, _ in failed:
                print(f"  FAILED {name}: expected {expected!r}, got {actual!r}")
        print(f"application version: {app_version!r}")
        if isp_version is not None:
            print(f"bootloader version: {isp_version!r}")
        return 0 if not failed else 1

    fw = open_firmware(args.firmware, args.convert, args.kb) if args.firmware else None

    print("== application mode ==")
    dev = find_device(want_isp=False)
    vid, pid = dev["vendor_id"], dev["product_id"]
    print(f"  found 0x{vid:04X}:0x{pid:04X} {dev.get('product_string') or ''}")
    check("enumerated pid is a known application pid", True, pid in APP_PIDS)
    check("device path", True, bool(dev.get("path")))

    t = HidTransport(vid, pid)
    t.open()
    try:
        status, payload = t.request(CMD_APP_VERSION)
    finally:
        t.close()
    app_version = payload.decode("ascii", "replace")
    check("0xB0 status", 0, status)
    print(f"  application version: {app_version!r}")

    if fw is not None and fw.is_isp:
        # A bootloader image is not flashed by the ISP-mode sequence (`flash`
        # refuses it): the update runs from application mode through
        # flash-bootloader's own auth.  Verify the part that gates that route
        # -- 0xA5 with the plaintext GF payload -- and stop here; the
        # ISP-mode section below would only test the wrong route.
        print("\n== application mode: bootloader-update pre-flight "
              "(flash-bootloader route) ==")
        t = HidTransport(vid, pid)
        t.open()
        try:
            status, _ = t.request(CMD_CHECK_PROFILE, APP_AUTH_PAYLOAD)
        finally:
            t.close()
        check("0xA5 auth (plaintext GF) accepted", 0, status)
        if status == 0:
            print("  the keyboard will authenticate, erase LDROM, program the "
                  "new image and verify it before rebooting")
        print("  (ISP-mode section skipped: bootloader images are updated by "
              "flash-bootloader, not flash)")
        return summarize(app_version)

    print("\n== enter ISP via 0xA0 ==")
    t = HidTransport(vid, pid)
    t.open()
    try:
        status, payload = t.request(CMD_ENTER_ISP)
    finally:
        t.close()
    check("0xA0 status", 0, status)
    target_kb = payload[0] if payload else 0
    model = ((payload[1] << 8) | payload[2]) if len(payload) >= 3 else 0
    print(f"  target {target_kb} KB, model 0x{model:04X}")
    check("0xA0 target size", 32, target_kb)
    check("0xA0 model", 0x0000, model)

    isp = wait_for_isp_device(timeout=args.timeout)
    print(f"\n== ISP mode: 0x{isp['vendor_id']:04X}:0x{isp['product_id']:04X} "
          f"{isp.get('product_string') or ''} ==")
    if isp["product_id"] not in ISP_MODE_PIDS:
        raise FlashError(
            f"unexpected ISP pid: got 0x{isp['product_id']:04X}, "
            f"expected one of {', '.join(f'0x{p:04X}' for p in sorted(ISP_MODE_PIDS))}"
        )

    t2 = HidTransport(isp["vendor_id"], isp["product_id"])
    t2.open()
    try:
        status, payload = t2.request(CMD_ISP_VERSION)
        isp_version = payload.decode("ascii", "replace")
        check("0xA6 status", 0, status)
        print(f"  bootloader version: {isp_version!r}")
        check("bootloader is B2175V13", "B2175V13", isp_version)

        if fw is not None:
            prof = fw.raw[-14:-4]
            st, rsp = t2.request(CMD_CHECK_PROFILE, prof)
            print(f"  0xA5 {describe_image(fw)}: status=0x{st:02X} echo={rsp.hex(' ')}")
            # Must be 0x00: a rejected profile blocks 0xA4 erase.
            check("0xA5 profile accepted", 0, st)
    finally:
        t2.close()

    app = reset_to_app_mode(timeout=args.timeout)
    print(f"\n== reset via 0xAF -> application mode ==")
    check("back on application pid", True, app["product_id"] in APP_PIDS)

    if not args.no_latch:
        print("\n== 0xAA latch test (state-changing, self-recovering) ==")
        t3 = HidTransport(app["vendor_id"], app["product_id"])
        t3.open()
        try:
            status, payload = t3.request(CMD_ENTER_LDROM_ISP)
        finally:
            t3.close()
        check("0xAA status", 0, status)
        aa_payload = payload.hex(" ") if payload else ""
        # Byte 0 is the target size in KB, bytes 1-2 the running application PID.
        # So the third byte tracks the firmware variant rather than being fixed:
        # measured "04 01 40" on S1947V33 (PID 0x0140) and "04 01 41" on
        # S2175V16 (PID 0x0141).
        aa_expected = f"04 01 {app['product_id'] & 0xFF:02X}"
        print(f"  0xAA payload: {aa_payload}  (expected {aa_expected} = 4 KB)")
        check("0xAA payload is the 4 KB loader", aa_expected, aa_payload)
        # 0xAA reports success but never switches modes on this hardware, so
        # the check passes when NO ISP device has appeared.
        check("0xAA did not reach ISP mode", True, not find_device_isp_present())

        print("  recovering with 0xAF ...")
        app = reset_to_app_mode(timeout=args.timeout)

        # The keyboard re-enumerates after the reset, so the first 0xA0 can land
        # on a stale handle.  Retry until the device answers with its real
        # target size, which also proves it genuinely entered ISP mode.
        recovered_kb = None
        for attempt in range(4):
            t4 = HidTransport(app["vendor_id"], app["product_id"])
            t4.open()
            try:
                status, payload = t4.request(CMD_ENTER_ISP)
                recovered_kb = payload[0] if payload else 0
            except FlashError as exc:
                print(f"    attempt {attempt + 1}: {exc}")
                time.sleep(1.0)
                app = reset_to_app_mode(timeout=args.timeout)
                continue
            finally:
                t4.close()
            break
        print(f"  0xA0 after recovery: {recovered_kb} KB")
        check("0xA0 restored after latch", 32, recovered_kb)

        # A successful 0xA0 above genuinely enters ISP mode, so leave the
        # keyboard in application mode rather than stranding it in ISP.
        if recovered_kb is not None:
            wait_for_isp_device(timeout=args.timeout)
            app = reset_to_app_mode(timeout=args.timeout)
        else:
            app = reset_to_app_mode(timeout=args.timeout)

    t5 = HidTransport(app["vendor_id"], app["product_id"])
    t5.open()
    try:
        status, payload = t5.request(CMD_APP_VERSION)
    finally:
        t5.close()
    check("0xB0 still healthy at end", app_version,
          payload.decode("ascii", "replace"))

    return summarize(app_version, isp_version)


def find_device_isp_present() -> bool:
    """True when an ISP-mode device is currently enumerated."""
    try:
        find_device(want_isp=True)
        return True
    except FlashError:
        return False


def cmd_reset(args) -> int:
    """Send 0xAF and wait for the keyboard to return to application mode.

    The software equivalent of unplug/replug.  Needed after a latched 0xAA, and
    after `flash --dry-run`, which enters ISP mode but skips the reset.
    """
    dev = reset_to_app_mode(timeout=args.timeout)
    print(f"keyboard is back in application mode: "
          f"0x{dev['vendor_id']:04X}:0x{dev['product_id']:04X} "
          f"{dev.get('product_string') or ''}")
    return 0


def _hidapi_hint(exc: BaseException) -> str:
    """Actionable advice for a failed `import hid`.

    `hidapi` is a hard dependency, so a failure here is nearly always the native
    half missing rather than the Python package: the wheel built fine but the
    shared library it dlopen()s is absent.  The package name is only worth
    suggesting if `hid` genuinely is not installed, e.g. someone running the
    single file straight out of a checkout with no dependencies.
    """
    if isinstance(exc, ModuleNotFoundError) and (exc.name or "").split(".")[0] == "hid":
        return (
            "hidapi is a required dependency and is not installed.\n"
            "Install it with:  pip install hyisp-util\n"
            "If you are running this file directly from a checkout, use the venv "
            "in that directory instead."
        )
    return (
        "hidapi is installed but its native USB library could not be loaded.\n"
        "This is a system dependency, not a Python one:\n"
        "  Debian/Ubuntu:  sudo apt install libusb-1.0-0 libudev1\n"
        "  Fedora/RHEL:    sudo dnf install libusbx systemd-libs\n"
        "  Arch:           sudo pacman -S libusb libsystemd\n"
        "  macOS:          brew install libusb\n"
        "Then reinstall the wheel:  pip install --force-reinstall hidapi"
    )


def cmd_probe(args) -> int:
    """Read-only interrogation: getFirmwareVersion only.  Never writes.

    Sends 0xB0 (application version) and, in ISP mode, 0xA6 (ISP version).
    Neither command touches flash, so this is safe to run on a live keyboard.
    """
    import hid

    dev = find_device(want_isp=args.allow_isp)
    in_isp = dev["product_id"] in ISP_MODE_PIDS
    print(f"device 0x{dev['vendor_id']:04X}:0x{dev['product_id']:04X} "
          f"{dev.get('product_string') or ''} ({'ISP mode' if in_isp else 'application mode'})")

    t = HidTransport(dev["vendor_id"], dev["product_id"])
    t.open()
    try:
        # 0xA6 is nominally the ISP-version query, but the application firmware
        # answers it too, so probe both regardless of the enumerated mode.
        probes = [(CMD_APP_VERSION, "0xB0 application version"),
                  (CMD_ISP_VERSION, "0xA6 ISP version")]
        for cmd, label in probes:
            print(f"  {label}")
            frame = build_frame(cmd)
            print(f"    frame {frame.hex(' ')}")
            t.send_frame(frame)
            try:
                rsp = t.recv_frame(timeout=args.timeout)
            except FlashError as exc:
                print(f"    no response: {exc}")
                continue
            print(f"    raw response {rsp.hex(' ')}")
            try:
                rcmd, status, payload = parse_response(rsp)
                print(f"    cmd=0x{rcmd:02X} status=0x{status:02X} payload={payload.hex(' ')}")
                if rcmd == cmd and status == 0:
                    for i, b in enumerate(payload):
                        print(f"      byte {i}: 0x{b:02X} ({b})")
            except FlashError as exc:
                print(f"    undecodable: {exc}")
    finally:
        t.close()
    return 0


def decode_config0(word: int) -> list[str]:
    """Decode the NUC123 CONFIG0 word (0x00300000) with TRM V1.06 bit names.

    Bits are read from the big-endian value the device reports: the 0xFB
    handler emits each byte of read32(0x00300000) MSB-first, so the payload's
    first byte is bits 31:24 -- no byte-pair swap is needed (unlike 0xB8).
    Only the bits the TRM names in V1.06 are decoded; 19:8, 5:3 are Reserved.
    """
    cbs = (word >> 6) & 0x3
    lock = (word >> 1) & 0x1
    out: list[str] = []
    out.append(
        f"CWDTEN={(word >> 31) & 1} "
        f"({(word >> 31) & 1 and 'WWDT disabled at power-on' or 'WWDT enabled at power-on'})"
    )
    cfosc = (word >> 24) & 0x7
    cfosc_meaning = {
        0x0: "external crystal 4-24 MHz",
        0x1: "external 32.768 kHz",
        0x2: "internal 22.1184 MHz",
        0x3: "internal 10 kHz",
        0x7: "internal 22.1184 MHz (RC)",
    }.get(cfosc, "reserved")
    out.append(f"CFOSC={cfosc:03b} ({cfosc_meaning})")
    cboden = (word >> 23) & 1
    out.append(f"CBODEN={cboden} (BOD {'disabled' if cboden else 'enabled'} after POR)")
    out.append(f"CBOV={(word >> 21) & 0x3:02b}")
    cbost = (word >> 20) & 1
    out.append(f"CBORST={cbost} (BOD reset {'disabled' if cbost else 'enabled'})")
    out.append(f"CBS={cbs:02b} (boot {'APROM' if cbs & 0x2 else 'LDROM'})")
    out.append(f"DFVSEN={(word >> 2) & 1} (fixed 4 KB data flash)")
    out.append(f"LOCK={lock} ({'unlocked' if lock else 'locked (ICP/debug readout blocked)'})")
    return out


def cmd_regs(args) -> int:
    """Read-only: dump the CONFIG0/CONFIG1 register words via 0xFB and 0xB8.

    Sends only read-only getters (0xFB user config, 0xB8 GCR PDID, 0xB9 flash
    base).  Nothing is erased, written or locked, so this is safe on a live
    keyboard.  Application mode implements all of them; 0xFB alone recovers the
    locked/unlocked state that 0xA8 would change.
    """
    import hid

    dev = find_device(want_isp=args.allow_isp)
    in_isp = dev["product_id"] in ISP_MODE_PIDS
    print(f"device 0x{dev['vendor_id']:04X}:0x{dev['product_id']:04X} "
          f"{dev.get('product_string') or ''} ({'ISP mode' if in_isp else 'application mode'})")

    t = HidTransport(dev["vendor_id"], dev["product_id"])
    t.open()
    try:
        status, payload = t.request(CMD_USER_CONFIG)
        if status != 0 or len(payload) < 8:
            raise FlashError(
                f"0xFB user-config read failed (status 0x{status:02X}, "
                f"payload {payload.hex(' ')})"
            )
        # MSB-first emission: the payload already is CONFIG0 then CONFIG1 in
        # big-endian byte order.
        config0 = int.from_bytes(payload[0:4], "big")
        config1 = int.from_bytes(payload[4:8], "big")
        print(f"  CONFIG0 (0x00300000) = 0x{config0:08X}")
        for line in decode_config0(config0):
            print(f"    {line}")
        print("    (note: on a DK4 the raw word reads 0xFFFFFF78; the CBS=01 "
              "decode means POR enters the LDROM, which then chains to the app "
              "-- not a contradiction. Decode is per-TRM.)")
        print(f"  CONFIG1 (0x00300004) = 0x{config1:08X} (DFBADR={config1 & 0xFFFFF:#x})")

        # 0xB8 GCR: byte-pair-swapped delivery, so the raw payload is already
        # the display order (PDID 0x00012315 -> 00 01 23 15).
        status, payload = t.request(CMD_READ_PDID)
        if status == 0 and len(payload) >= 4:
            pid = int.from_bytes(payload[0:4], "big")
            print(f"  GCR PDID  (0x50000000) = 0x{pid:08X} "
                  f"(payload {payload[:4].hex(' ')})")

        status, payload = t.request(CMD_FLASH_BASE)
        if status == 0 and len(payload) >= 4:
            base = int.from_bytes(payload[0:4], "big")
            print(f"  flash base (0x2000013C) = 0x{base:08X}")
    finally:
        t.close()
    return 0


def _int_arg(text: str) -> int:
    try:
        return int(text, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from exc


def cmd_read(args) -> int:
    """Dump flash with 0xA7 -- read-only, never writes a byte.

    Enters ISP mode from application mode when needed (the running application
    does not implement 0xA7), reads the requested window in READ_CHUNK-byte
    units, and -- unless --stay-in-isp -- resets the keyboard back to
    application mode before returning, so it is usable again afterwards.
    """
    start, length = args.start, args.length
    if start < 0 or length <= 0:
        raise FlashError("--start must be >= 0 and --length must be > 0")
    if start + length > 0x10000:
        raise FlashError(
            f"window 0x{start:04X}..0x{start + length - 1:04X} does not fit in "
            "the 16-bit address space of 0xA7 (0x0000..0xFFFF)"
        )
    if os.path.exists(args.output) and not args.force:
        raise FlashError(
            f"{args.output} already exists; pass --force to overwrite it"
        )

    dev, already_in_isp = find_device_for_flash()
    print(f"found 0x{dev['vendor_id']:04X}:0x{dev['product_id']:04X} "
          f"{dev.get('product_string') or ''}")

    if already_in_isp:
        isp = dev
        print("  already in ISP mode; skipping the 0xA0 entry step")
    else:
        t = HidTransport(dev["vendor_id"], dev["product_id"])
        t.open()
        try:
            status, payload = t.request(CMD_ENTER_ISP)
            if status != 0:
                raise FlashError(f"Enter ISP mode fail (status 0x{status:02X})")
            kb = payload[0] if payload else 0
            model = ((payload[1] << 8) | payload[2]) if len(payload) >= 3 else 0
            progress.step(f"0xA0 enter ISP: target {kb} KB, model 0x{model:04X}")
        finally:
            t.close()
        print("  waiting for the keyboard to re-enumerate in ISP mode...")
        isp = wait_for_isp_device(timeout=args.timeout)

    t = HidTransport(isp["vendor_id"], isp["product_id"])
    t.open()
    try:
        status, ver = t.request(CMD_ISP_VERSION)
        if status == 0:
            progress.step(
                f"ISP version: {ver.split(b'\\x00')[0].decode('ascii', 'replace')}"
            )
        progress.step(
            f"0xA7 reading {length} byte(s) from 0x{start:04X} "
            f"in {READ_CHUNK}-byte requests"
        )
        data = bytearray()
        next_report = 4096
        t0 = time.monotonic()
        for off in range(0, length, READ_CHUNK):
            addr = start + off
            n = min(READ_CHUNK, length - off)
            req = bytes((addr >> 8, addr & 0xFF, n))
            last: Optional[FlashError] = None
            for _attempt in range(READ_RETRIES):
                try:
                    status, chunk = t.request(CMD_READ_FLASH, req)
                except FlashError as exc:
                    # Mid-fill read: the frame was sampled before its payload
                    # was complete, so the XOR check failed.  0xA7 is
                    # idempotent -- simply ask again.
                    last = exc
                    continue
                if status != 0:
                    raise FlashError(
                        f"0xA7 read at 0x{addr:04X} failed "
                        f"(status 0x{status:02X})"
                        + (" -- 0xA7 is only implemented in ISP mode"
                           if status == 0xF1 else "")
                    )
                if len(chunk) < n:
                    last = FlashError(f"short payload: {len(chunk)} of {n} bytes")
                    continue
                data.extend(chunk[:n])
                break
            else:
                raise FlashError(
                    f"0xA7 read at 0x{addr:04X} failed after {READ_RETRIES} "
                    f"attempts ({last})"
                )
            if len(data) >= next_report:
                progress.step(f"read {len(data)} of {length} byte(s)")
                next_report += 4096
        elapsed = time.monotonic() - t0
        digest = hashlib.sha256(bytes(data)).hexdigest()
    finally:
        t.close()

    with open(args.output, "wb") as fh:
        fh.write(data)
    rate = len(data) / elapsed if elapsed > 0 else 0.0
    print(f"\nread {len(data)} byte(s) from 0x{start:04X} in {elapsed:.1f} s "
          f"({rate:.0f} B/s)")
    print(f"sha256 {digest}")
    print(f"wrote {args.output}")

    if args.stay_in_isp:
        print("leaving the keyboard in ISP mode (--stay-in-isp); run "
              "`hyisp-util reset` to return it to normal mode")
    else:
        dev = reset_to_app_mode(timeout=args.timeout)
        print(f"keyboard is back in application mode: "
              f"0x{dev['vendor_id']:04X}:0x{dev['product_id']:04X} "
              f"{dev.get('product_string') or ''}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="HYISP (Heng Yu ISP) firmware flasher -- validated on the "
                    "Das Keyboard 4 (Professional, Ultimate, Professional for Mac)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[-1] if __doc__ else None,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_kb_option(parser):
        parser.add_argument(
            "--kb",
            type=int,
            default=APPLICATION_IMAGE_SIZE // 1024,
            help=f"target image size in KB when wrapping a bare image (default "
                 f"{APPLICATION_IMAGE_SIZE // 1024}, the size a DK4 reports for "
                 "its application region)",
        )
        return parser

    def add_convert_options(parser):
        """Shared by every subcommand that can accept a bare image."""
        parser.add_argument(
            "--convert",
            action="store_true",
            help="treat FIRMWARE as a bare (plain, unpadded) image and wrap it in "
                 "the vendor container in memory before using it; nothing is "
                 "written to disk",
        )
        add_kb_option(parser)
        return parser

    sp = sub.add_parser("list", help="list matching HYISP device interfaces")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("info", help="parse and validate a firmware file")
    sp.add_argument("firmware")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser(
        "convert",
        help="wrap a bare firmware image in the vendor container",
        description="Convert a plain, unpadded firmware image -- as shipped by "
                    "community DK4 projects -- into the obfuscated, padded, "
                    "whole-KB form the device requires, with the metadata trailer "
                    "and balanced checksum added. The output is then "
                    "indistinguishable in structure from a vendor image and can be "
                    "flashed with `flash` or with the vendor's own tools. No "
                    "hardware is touched.",
    )
    sp.add_argument("firmware")
    sp.add_argument("-o", "--output", help="output path "
                    "(default: <input>.vendor.bin)")
    add_kb_option(sp)
    sp.add_argument("--force", action="store_true",
                    help="overwrite the output and skip the vector-table check")
    sp.add_argument("--verbose", action="store_true",
                    help="log every mock-device command")
    sp.set_defaults(func=cmd_convert)

    sp = add_convert_options(
        sub.add_parser("selftest", help="run the sequence against a mock device")
    )
    sp.add_argument("firmware")
    sp.add_argument(
        "--windows-profile",
        action="store_true",
        help="send the Windows-transformed profile, which this bootloader rejects",
    )
    sp.set_defaults(func=cmd_selftest)

    sp = sub.add_parser("probe", help="read-only: query firmware version (never writes)")
    sp.add_argument("--allow-isp", action="store_true",
                    help="accept a device already in ISP mode (adds the 0xA6 query)")
    sp.add_argument("--timeout", type=float, default=3.0, help="per-command timeout")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser(
        "regs",
        help="read-only: dump CONFIG0/CONFIG1 and chip registers (never writes)",
        description="Read the on-chip register words through the bootloader's "
                    "read-only getters and decode them: 0xFB returns CONFIG0 "
                    "(user configuration: WWDT, clock source, BOD, boot select "
                    "CBS, LOCK) and CONFIG1 (data-flash base); 0xB8 returns the "
                    "GCR PDID; 0xB9 returns the RAM flash-base global. Nothing "
                    "is erased, written or locked, so this is safe on a live "
                    "keyboard and is the way to read the LOCK state that 0xA8 "
                    "would change.",
    )
    sp.add_argument("--allow-isp", action="store_true",
                    help="accept a device already in ISP mode")
    sp.set_defaults(func=cmd_regs)

    sp = sub.add_parser(
        "read",
        help="read-only: dump flash via 0xA7 (never writes)",
        description="Read a window of flash with the bootloader's 0xA7 read "
                    "command and save it to a file. Nothing is erased or "
                    "written. The running application does not implement 0xA7, "
                    "so read enters ISP mode with 0xA0 first when needed and "
                    "returns the keyboard to normal operation when done. "
                    "Responses are requested in 10-byte units because the "
                    "feature-report channel delivers at most 15 response bytes "
                    "per read; failed checksums are retried automatically.",
    )
    sp.add_argument("-o", "--output", default="flash_dump.bin",
                    help="output path (default: flash_dump.bin)")
    sp.add_argument("--start", type=_int_arg, default=0,
                    help="start address, hex (0x...) or decimal (default 0)")
    sp.add_argument("--length", type=_int_arg, default=APPLICATION_IMAGE_SIZE,
                    help="bytes to read, hex or decimal (default "
                         f"{APPLICATION_IMAGE_SIZE}, the whole application image)")
    sp.add_argument("--force", action="store_true",
                    help="overwrite the output file if it exists")
    sp.add_argument("--stay-in-isp", action="store_true",
                    help="leave the keyboard in ISP mode instead of resetting "
                         "it back to application mode")
    sp.add_argument("--timeout", type=float, default=20.0,
                    help="re-enumeration / reset timeout in seconds")
    sp.set_defaults(func=cmd_read)

    sp = add_convert_options(
        sub.add_parser(
            "verify",
            help="full read-only conformance check; writes nothing",
            description="Run every non-writing check against a connected keyboard. "
                        "Nothing is erased or written. The keyboard is moved between "
                        "application and ISP mode, and unless --no-latch is given the "
                        "0xAA latch is triggered deliberately and then recovered from, "
                        "which is the only way to prove recovery works.",
        )
    )
    sp.add_argument("firmware", nargs="?",
                    help="image whose profile is used for the 0xA5 check (optional)")
    sp.add_argument("--no-latch", action="store_true",
                    help="skip the deliberate 0xAA latch and recovery test")
    sp.add_argument("--timeout", type=float, default=20.0,
                    help="re-enumeration / reset timeout in seconds")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser(
        "reset",
        help="send 0xAF and wait for the keyboard to return to normal mode",
        description="Software equivalent of unplug/replug. Use this if the "
                    "keyboard is stuck in ISP mode, after `flash --dry-run`, or "
                    "after a latched 0xAA.",
    )
    sp.add_argument("--timeout", type=float, default=20.0,
                    help="re-enumeration / reset timeout in seconds")
    sp.set_defaults(func=cmd_reset)

    sp = add_convert_options(
        sub.add_parser("flash", help="flash a firmware file to a keyboard")
    )
    sp.add_argument("firmware")
    sp.add_argument("--chunk", type=int, default=DEFAULT_CHUNK,
                    help=f"write chunk size (default {DEFAULT_CHUNK})")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the sequence without writing anything")
    sp.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sp.set_defaults(func=cmd_flash)

    sp = sub.add_parser(
        "flash-bootloader",
        help="replace the 4 KB LDROM bootloader via the application flash path",
        description="Update the keyboard's bootloader (LDROM) while it runs "
                    "normally from application mode. The application firmware "
                    "implements a second flash path whose commands address the "
                    "4 KB bootloader window: auth (0xA5 plaintext GF), erase "
                    "(0xA4), program (0xA1, image deobfuscated by the device), "
                    "latch (0xAA), then a verify-and-reset (0xAF) that only "
                    "reboots after the device confirms the HYISP signature and "
                    "checksum of the new image. A failed verify leaves the "
                    "keyboard in application mode and the write can be retried. "
                    "Accepts vendor-container bootloader images (A2175V13.bin) "
                    "or plaintext dumps read back from flash. The keyboard must "
                    "be in application mode; afterwards the new bootloader is "
                    "entered once via 0xA0 to confirm it boots, and the keyboard "
                    "is returned to application mode unless --stay-in-isp.",
    )
    sp.add_argument("firmware")
    sp.add_argument("--chunk", type=int, default=BOOTLOADER_WRITE_CHUNK,
                    help=f"write chunk size (default {BOOTLOADER_WRITE_CHUNK})")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the sequence without writing anything")
    sp.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sp.add_argument("--stay-in-isp", action="store_true",
                    help="leave the keyboard in ISP mode after a successful "
                         "update instead of resetting it back to application mode")
    sp.add_argument("--timeout", type=float, default=20.0,
                    help="re-enumeration / reset timeout in seconds")
    sp.set_defaults(func=cmd_flash_bootloader)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except FlashError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    except ImportError as exc:
        print(f"\nerror: {exc}\n{_hidapi_hint(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
