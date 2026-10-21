# HYISP Firmware Update Protocol

Notes on the USB HID vendor firmware-update protocol used by the Heng Yu (HYISP)
bootloader, as found on the Das Keyboard 4 — Professional, Ultimate and
Professional for Mac, all of which present as `D4215` on USB `0x24F0` — to receive
application firmware updates. Written while building `hyisp.py`.

Other Das Keyboard models use a different firmware family and a different update
mechanism; nothing here applies to them.

Every behavioural claim below was confirmed against physical DK4 hardware. Where
the two vendor updaters disagree, the hardware result is the authority and the
implementer's own observations are flagged as such.

Vendor firmware images are not distributed here. Obtain them from the vendor's
official download.

---

## 1. Device identification

The keyboard exposes a vendor-specific HID collection alongside its normal
boot-keyboard interface. Updaters find it by matching:

```
kIOHIDVendorIDKey         = VID            0x24F0
kIOHIDPrimaryUsageKey     = 0x80   (128)
kIOHIDPrimaryUsagePageKey = 0x01   (Generic Desktop)
```

Usage `0x80` in the Generic Desktop page selects the vendor control collection
rather than the keyboard collection.

**Product IDs:**

| PID | State |
|---|---|
| `0x0000` | ISP (bootloader) mode, shown as `"ISP Mode"` |
| `0x0140` | application firmware, Windows keymap |
| `0x0141` | application firmware, macOS keymap |

> **The application PID identifies the firmware variant, not the hardware.**
> Measured by flashing each image to a DK4 and re-reading the USB descriptor:
>
> | image | running firmware | enumerates as |
> |---|---|---|
> | `L1947V33.bin` | `S1947V33` | `0x24F0:0x0140` |
> | `L2175V16.bin` | `S2175V16` | `0x24F0:0x0141` |
>
> Which variant is Windows and which is macOS is settled by **provenance, not by
> inference**: `L1947V33.bin` ships in the archive containing the vendor's Windows
> flashing utility and `L2175V16.bin` ships in the archive containing the macOS
> utility. The hardware is identical; only the emitted keycodes differ. This has
> been confirmed functionally by using a Mac build on the keyboard.
>
> The two images carry the identical `GFD4215` profile and report the same `D4215`
> hardware name, which is what makes the PID a variant marker: it changes on every
> application update. **Never key discovery off a fixed application PID.** Match on
> vendor ID plus usage page `0x01` / usage `0x80`, which is stable across variants.
> The `0xAA` response echoes the running PID too (§3.1), so its third byte varies
> for the same reason.
>
> Corroborating (not decisive) evidence comes from the HID report descriptors
> embedded in the images. Both declare the *same* physical keyboard — usage page
> `0x07`, usage `0xE0`, 231 keys (`0xE7`), 8-byte boot report, byte-identical
> vendor/consumer section. Only the declared usage ranges differ:
>
> | | `S1947V33` / `0x0140` (Windows) | `S2175V16` / `0x0141` (macOS) |
> |---|---|---|
> | usage max / report count | `0x67` / 104 | `0x6F` / 112 |
> | logical max | `0x65` (ASCII 101) | `0x6A` |
> | consumer usage | `0xB7` (Mute) | `0xB8` (Play) |
>
> `S2175V16` declares usages up to `0x6F`, i.e. `0x68`–`0x6F`: non-US backslash
> and pipe, non-US hash and spacebar, ISO Enter, 102/86 backslash, Finnish `Ro`,
> kana, yen, convert — the range a Mac layout needs. `S1947V33` stops at `0x67`,
> a plain US layout. On its own the descriptor cannot identify which build is the
> Mac one, since inferring the assignment from the usage range assumes the
> conclusion it would support. The packaging is the source of truth; the descriptor
> is corroboration only.
>
> > **`0xA5` does not distinguish the two variants.** Both carry the identical
> > `GFD4215` profile, so the bootloader accepts a Windows image on a
> > currently-macOS unit and vice versa without complaint. There is no device-side
> > guard against flashing the wrong OS variant; the symptom is misplaced keys,
> > not a failed update. `hyisp.py` warns when the running PID disagrees with
> > the image's known variant, and `info` prints the expected variant and PID.

Related vendor VID/PID pairs seen on sibling keyboards: `0x058F`, `0x0F39`,
`0x0A34`. The updaters accept `0x0F39` and `0x0A34` too, but none of the analysis
here depends on them.

---

## 2. HID transport layer

**Report size:** 8 bytes total = 1 report-ID byte + 7 payload bytes.

**Outgoing (host → keyboard):** every frame is split into `ceil(n / 7)` reports.
Report *i* is `[0x01][frame[7i .. 7i+6]]`, last report zero-padded. The fixed
leading `0x01` is the report ID. Sends use `HidD_SetFeature` on Windows and
`IOHIDDeviceSetReport(dev, 2, 1, buf, 8)` on macOS (type 2 = Output, ID 1).

**Incoming (keyboard → host):** poll the **feature** channel (`HidD_GetFeature` /
`IOHIDDeviceGetReport`) until a report arrives whose first byte is `0xED`, then
reassemble the consecutive reports into one buffer.

Verified on live DK4 hardware with hidapi:

- Replies arrive on the **feature** channel. `read()` on the vendor collection
  never returns a reply and only ever sees keystrokes.
- Requesting more than 8 bytes returns the **whole** frame in one call while it
  fits in the 15 bytes a single read delivers: a `0xB0` reply arrives as a
  single 13-byte `0xED…` frame, zero-padded beyond its end. Longer replies span
  successive reads instead — only `0xA7` produces those (§6.6).
- An idle keyboard yields a zero-filled buffer rather than an empty read, so the
  buffer must be scanned for the `0xED` magic and discarded when absent.

**Timeouts:** the macOS updater sleeps 10 ms between polls and fails after 300
retries (3 s). The Windows updater spawns a reader thread and waits on an event
with a 5 s timeout. A ~3 s poll loop is adequate in practice.

---

## 3. Frame format

Identical in both updaters. All integers are big-endian where multi-byte.

```
Host → keyboard            Keyboard → host
+---------------+          +---------------+
| 0  0xEA magic |          | 0  0xED magic |
| 1  len       |          | 1  len       |
| 2  cmd       |          | 2  cmd (echo)|
| 3  payload…  |          | 3  status     |
| …            |          | 4  payload…  |
| N  XOR8      |          | …            |
+---------------+          | N  XOR8      |
   len = payload_len + 2   +---------------+
```

`len` covers bytes 2..len inclusive, i.e. `len = 1 (cmd) + payload_len + 1`.
The **XOR-8 checksum is the byte at index `len + 1`** and is the XOR of bytes
`0 .. len`.

```
enter ISP       EA 02 A0 48      0xEA^0x02^0xA0 = 0x48
enter LDRom ISP EA 02 AA 42      0xEA^0x02^0xAA = 0x42
erase chip      EA 02 A4 4C      0xEA^0x02^0xA4 = 0x4C
protect chip    EA 02 A8 40      0xEA^0x02^0xA8 = 0x40
reset keyboard  EA 02 AF 47      0xEA^0x02^0xAF = 0x47
get version     EA 02 B0 58      0xEA^0x02^0xB0 = 0x58
get ver (ISP)   EA 02 A6 4E      0xEA^0x02^0xA6 = 0x4E
```

**Response status byte** (index 3) is `0` on success. Transport-level failures use
`0x15` (send failed), `0x16` (receive timed out), `0x17` (continuation read
failed) and `0x18` (checksum mismatch).

A response payload is present only for the echoed commands `0xA0`, `0xA5`, `0xA6`,
`0xAA`, `0xB0`, `0xB1` and `0xD0`.

> `0xB1` and `0xD0` are accepted but never sent by either updater. `0xB1` is
> answered by the application with its model id; `0xD0` is implemented by
> neither stage and answers `0xF1` (§4.1).

### 3.1 Measured behaviour of `0xAA` and `0xA5` on real hardware

Verified on a DK4 running bootloader `B2175V13`. The `0xAA` behaviour and the
ISP-mode `0xA5` encoding sweep were reproduced exactly on **two independent DK4
units**, so those are properties of the firmware, not of any one board; the
application-mode `0xA5` observation further down was measured on one unit.

**`0xAA` does not reach ISP mode from application mode.** It answers
`status 0x00` with payload `04 01 40` (4 KB, then the running application PID —
`04 01 41` once `S2175V16` is installed; see §1), consistent with the 4 KB loader,
but the device re-enumerates straight back to application mode. It is *not* an
alternative to `0xA0`. What it is — established in §6.5 — is the **commit/latch
step of the application-side bootloader write path**: sent after a completed
erase+program sequence, it arms `0xAF` to verify the new LDROM image before
resetting.

Sent bare, with no write sequence in front of it, `0xAA` **still latches the
device**: afterwards `0xA0` also stops working, answering the same `04 01 40`
instead of its usual `20 00 00`, and it no longer re-enumerates. The keyboard
keeps working normally (`0xB0` still answers `S1947V33`).

**Recovery is a plain `0xAF` reset — no power cycle needed.** Confirmed on both
units: with the device latched, sending `0xAF` (which answers `status 0x00`)
restores `0xA0` → `20 00 00`, and a following `0xA0` genuinely re-enters ISP mode.
Unplugging also works, which makes an unplug look like the only remedy when it is
not.

Issuing `0xAA` from *inside* ISP mode is rejected outright with `status 0xF1`: the
bootloader implements no such command, consistent with each stage only ever
writing the *other* stage's window (§6.5).

> **Consequence for implementers: never send `0xAA` bare.** On its own it cannot
> do anything useful — its only effect is latching a working keyboard until
> someone issues `0xAF` or unplugs it. Both vendor updaters select it
> automatically for a 4 KB image, so that branch will be taken by any tool that
> mirrors them; it is a dead end *there* because those tools never perform the
> erase+program steps that give the command meaning. The bootloader-update route
> that does use it is described in §6.5; `hyisp-util flash-bootloader`
> implements it.

**`0xA5` requires the profile bytes RAW, and a rejected profile locks the flash.**

Measured on a DK4 running bootloader `B2175V13`, from **ISP mode**, sweeping
payload encodings and lengths:

| `0xA5` payload | status |
|---|---|
| raw obfuscated bytes, ≥ 8 bytes | **`0x00` — accepted** |
| raw obfuscated bytes, 0–7 bytes | `0x18` |
| nibble-swapped and/or bit-inverted bytes, any length 0–10 | `0x18` |
| deobfuscated plaintext | `0x18` |

The device enforces a minimum length of 8 before it compares anything, and it
compares **raw** bytes. It accepts the full 10-byte trailer, and answers with a bare
status byte and **no** echo payload, so the profile name cannot be read back off
the device — it has to come from the image file.

> **Consequence for implementers:** `0x18` from `0xA5` is *not* advisory and does
> *not* mean "unsupported". When the profile is not accepted, the subsequent `0xA4`
> erase fails with the **same** `0x18`, so a wrong profile payload locks out the
> entire update while leaving the existing firmware intact. Fail hard on any
> non-zero `0xA5` status. `hyisp.py` does exactly this.

**The running application applies the same rule to its own pool.** Measured from
application mode on a single unit — the route §6.5 authenticates through — the
application accepts the §5.0-transformed bytes of its own identifier pool
(`GFHYISP` → `8B 9B 7B 6A 6B CA FA`) and the plaintext bytes `GF`; it rejects the
bootloader's 10-byte trailer with `0x18`. Each stage therefore accepts the
inverted encoding only of *its own* pool string: the bootloader wants the
firmware trailer raw, the application wants `GF` (or the inverted `GFHYISP`). A
payload that authenticates one stage is refused by the other with `0x18`, and
the comparison is against the pool the *running* stage stores — which is why the
vendor trailer works from ISP mode and fails from application mode, and vice
versa.

---

## 4. Command table

The keyboard runs **two different firmwares**, and the same opcode means
different things in each of them:

- the **ISP bootloader** (LDROM, 4 KB, `B2175V13`), which runs in ISP mode and
  can only write the **application** region;
- the **application** (APROM, 32 KB, `S1947V33` / `S2175V16`), which runs in
  normal operation and can only write the **bootloader** region.

That last point is structural, not incidental: whichever stage is executing
addresses the *other* stage's window (`0x00000 +` from the bootloader,
`0x100000 +` from the application), so **neither stage can rewrite itself**.
An application update is written by the bootloader; a bootloader update is
written by the application (§6.5).

### 4.1 ISP-mode commands (the bootloader)

| Opcode | Name | Payload | Notes |
|---|---|---|---|
| `0xA0` | enter ISP | — | 3-byte response: flash KB + model id |
| `0xA1` | write flash | `addrHi addrLo data…` | 16-bit big-endian address, into the **application** window |
| `0xA4` | erase chip | — | erases the **application** window; fails with `0x18` if `0xA5` was rejected |
| `0xA5` | check profile | 10 bytes, **raw** | must be ≥ 8 bytes; the bootloader compares the raw bytes against its own pool (the firmware trailer) — the `GF` magic and the model id, both still obfuscated (§5.0) |
| `0xA6` | get version (ISP) | — | returns the bootloader version |
| `0xA7` | read flash | `addrHi addrLo count` | 16-bit big-endian address + byte count; read-only; see §6.6 |
| `0xA8` | protect chip | — | locks the flash against debug-interface readout by clearing the CONFIG0 LOCK bit — the same CONFIG0 bit the application clears (§4.3) |
| `0xAA` | latch bootloader write | — | rejected outright with `0xF1` — the dispatcher's range covers `0xA0`–`0xAF`, but this opcode has no handler |
| `0xAF` | reset keyboard | — | |

`0xA0` returns 3 bytes: byte 0 is the **expected flash size in KB**, bytes 1–2 are
a model identifier. Both updaters verify `fileSize == responseByte0 << 10`, i.e.
the file must be exactly `N × 1024` bytes for the device's `N` KB flash.

Opcodes the bootloader does not implement answer `0xF1` ("unknown command"). The
bootloader's dispatcher covers exactly `0xA0`–`0xAF`, so the in-range opcodes
without a handler — `0xA2`, `0xA3`, `0xA9`, `0xAB`–`0xAE` — and every opcode
outside that range (`0xB0`, `0xB1`, `0xD0`, `0xAA`, `0xFB`, …) all answer
`0xF1`.

### 4.2 Application-mode commands

The application implements the same `0xA0`–`0xAF` opcodes as the bootloader,
plus a set of read-only getters and a few mode switches that exist only here.

| Opcode | Name | Payload | Notes |
|---|---|---|---|
| `0xA0` | enter ISP | — | sets the boot-select bit and issues a system reset → the device re-enumerates as ISP; a latched device answers `04 01 <PID>` and does not reset (§3.1) |
| `0xA1` | write flash | `addrHi addrLo data…` | auth-gated; writes into the **bootloader** window — flash address `0x100000 + offset`, not a CPU address |
| `0xA4` | erase chip | — | auth-gated; erases the 4 KB bootloader window |
| `0xA5` / `0xB4` | authenticate | `GF` (plaintext) or the §5.0 encoding of the application's own pool (`GFHYISP` → `8B 9B 7B 6A 6B CA FA`) | sets the auth flag; `0xB4` is an alias of `0xA5` (same handler); the pool is the application's, not the firmware trailer (§3.1) |
| `0xA6` / `0xB0` | get version | — | the **application** version (`S1947V33` / `S2175V16`) |
| `0xA8` | protect chip | — | clears the LOCK bit in CONFIG0, i.e. locks the flash; a no-op on an already-locked unit |
| `0xAA` | latch bootloader write | — | sets a RAM latch — it does **not** enter ISP, and it breaks `0xA0` until an `0xAF` clears it (§3.1) |
| `0xAF` / `0xFF` | reset keyboard | — | CPU reset; when latched, verifies the new bootloader first (§6.5); `0xFF` is an alias of `0xAF` (same handler) |
| `0xB1` | model id | — | replies `D4215` |
| `0xB2` | sentinel | — | replies the single byte `0xC7` |
| `0xB7` | vector table | — | 12 bytes: the APROM reset vector and stack pointer |
| `0xB8` | GCR PDID | — | 4 bytes, byte-pair swapped (`00 01 23 15` = PDID `0x00012315`) |
| `0xB9` | flash base | — | 4 bytes, big-endian (`0x00008000`) |
| `0xF1` | byte program | `addrHi addrLo data…` | raw, into the **settings** region at `0x8000 +` |
| `0xF2` | byte read | `addrHi addrLo count` | from the settings region at `0x8000 +` |
| `0xF4` | product id | — | replies `40 01` = PID `0x0140`, little-endian |
| `0xF8` | seal settings | — | writes a checksum over the 4 KB settings region |
| `0xFB` | user config | — | 8 bytes: CONFIG0 then CONFIG1, each MSB-first (§4.5) |
| `0x68` | host command gate | — | arms the host key-injection path |
| `0x6F` / `0xF0` | mode on / off | — | USB descriptor switch |
| `0x28` | (inline) | — | acknowledged, no payload |

Opcodes the application does not implement answer `0xF1` — `0xA7` (read flash)
and `0xD0` among them. The full set the application answers is `0x28`, `0x68`,
`0x6F`, `0xA0`, `0xA1`, `0xA4`–`0xA6`, `0xA8`, `0xAA`, `0xAF`, `0xB0`–`0xB2`,
`0xB4`, `0xB7`–`0xB9`, `0xF0`–`0xF2`, `0xF4`, `0xF8`, `0xFB` and `0xFF`;
everything else — `0xA2`, `0xA3`, `0xA7`, `0xA9`, `0xAB`–`0xAE`, `0xD0` and
`0x00` — falls through to the default handler and answers `0xF1`.

`0xFB` is **application-only**: the bootloader implements no register getters
at all, and answers `0xF1`.

> **Consequence for implementers:** the "enter ISP" command is not a mode
> switch you can inspect the result of — it is a *reset request*. `0xA0` from
> the application sets a one-shot boot-select bit and resets the chip, and the
> bootloader takes over. The reply is the same 3 bytes the bootloader sends
> (`20 00 00`), so the reply tells you nothing about what the reset did — and on
> a latched device it tells you nothing at all, because there is no reset
> either (§3.1).

### 4.3 The same opcode in both stages

| Opcode | In ISP mode | In application mode |
|---|---|---|
| `0xA0` | replies `20 00 00` (32 KB target, model `0x0000`) | replies `20 00 00` and resets into the bootloader; a latched device answers `04 01 <PID>` and does not reset |
| `0xA1` | writes the **application** window, unauthenticated | writes the **bootloader** window, auth-gated by `0xA5` |
| `0xA4` | erases the **application** window | erases the **bootloader** window, and sets the CBS boot-origin bit first |
| `0xA5` | compares the raw firmware trailer (its own pool) | compares plaintext `GF` or the transform of its own pool (`GFHYISP` → `8B 9B 7B 6A 6B CA FA`) |
| `0xA6` | bootloader version `B2175V13` | application version `S1947V33` / `S2175V16` |
| `0xA7` | reads flash | `0xF1` — not implemented |
| `0xA8` | clears the LOCK bit in CONFIG0, i.e. locks the flash | clears the LOCK bit in CONFIG0 — the same operation |
| `0xAA` | `0xF1` — rejected outright | sets the RAM latch; replies `04 01 <PID>`; does not enter ISP (§3.1) |
| `0xAF` | resets the keyboard | CPU reset; verifies the new bootloader first if latched |

`0xB0`/`0xB1`/`0xD0` invert the pattern: `0xB0` and `0xB1` are application-only
(`0xF1` in ISP mode), and `0xD0` is implemented by neither.

### 4.4 Note on `0x00` — the "enter testing mode" command

The vendor's Windows tool sends a 5-byte frame `EA 03 00 1F 18` for a lone
`0x00` op code, mnemonic "enter testing mode". It has **no effect** on either
image, for two independent reasons:

1. **It fails the device's XOR check.** The checksum is the XOR of bytes `0 … len`
   (§3), so this frame should carry `XOR(EA,03,00,1F) = 0xF6`, not `0x18`. The
   frame is rejected before dispatch and the device answers **`0xFC`** (bad
   checksum).
2. **Even with a valid checksum, `0x00` is not implemented.** The application's
   dispatcher has no case for it (its lowest implemented op code is `0x28`), and
   the bootloader's dispatcher only routes `0xA0`–`0xAF`, so `0x00` falls
   through to the default handler in both → status **`0xF1`** (unknown command).

The mnemonic survives only in the vendor's host tool.

### 4.5 The register words: CONFIG0, CONFIG1 and the boot chain

The application exposes the chip's user-configuration words through `0xFB`,
which returns two 32-bit words, each emitted **MSB-first**, so the payload can
be read as big-endian directly (no byte-pair swap — unlike `0xB8`):

```
CONFIG0 (0x00300000)  4 bytes, MSB-first
CONFIG1 (0x00300004)  4 bytes, MSB-first
```

`hyisp-util regs` reads and decodes them. The bits that matter to an
implementer:

| Bit | Name | Meaning |
|---|---|---|
| 31 | CWDTEN | 0 = window watchdog enabled at power-on |
| 26:24 | CFOSC | CPU clock after reset (`111` = internal RC) |
| 23:20 | CBODEN / CBOV / CBORST | brown-out detector enable, voltage, reset enable |
| **7:6** | **CBS[1:0]** | **CBS[1]** (bit 7) = boot origin, **CBS[0]** (bit 6) = memory map (IAP) — see below |
| 2 | DFVSEN | 1 = fixed 4 KB data flash; 0 = variable size via DFBADR |
| **1** | **LOCK** | 0 = flash locked (external debug reads return `0xFFFFFFFF`); 1 = unlocked |
| 0 | DFEN | data-flash enable |

**CONFIG1** holds `DFBADR[19:0]`, the data-flash base address. On a DK4 it
reads `0x00008000` — the same value as the settings window that `0xF1`/`0xF2`
address.

A DK4 in normal operation reads:

```
CONFIG0 = 0xFFFFFF78   →  CBS = 01, LOCK = 0 (locked), DFVSEN = 0, CWDTEN = 1
CONFIG1 = 0x00008000   →  DFBADR = 0x8000
```

**LOCK = 0 (locked)** is the normal shipping state: `0xA8` only ever *clears*
the bit, so on an already-locked unit it changes nothing, and a live read of
`0xFB` before and after it is byte-identical. The lock blocks **external
debug-interface reads** (ICP/SWD readout, an anti-teardown measure); ISP reads
and flash execution are unaffected.

**CBS[1:0] selects the boot origin and the memory map.** CBS[1] (bit 7) picks
the boot origin; CBS[0] (bit 6) picks the post-boot memory map. The TRM's four
combinations:

| CBS[1:0] | Boot origin | Executing range / map |
|---|---|---|
| `11` | APROM | APROM only; the LDROM is not memory-readable — the production state |
| `01` | LDROM | LDROM only; the APROM is not memory-readable |
| `10` | APROM | APROM + LDROM mapped at `0x100000`–`0x10FFF` (IAP; the vector page is remappable) |
| `00` | LDROM | LDROM + most of the APROM (the first 512 B come from the LDROM at `0x0`; IAP) |

A DK4 reads `01`: the boot origin is the LDROM, so the bootloader runs first and
chains to the application. The *map* cell of the `01` row does not describe what
this firmware does, though — the application executes from the APROM at
`0x000000` (it serves the APROM reset vector through `0xB7`) and reaches the
bootloader window at flash `0x100000` (the `HYISP` signature at `0x100FF4`).
Treat the map column as indicative and the boot-origin column as
authoritative.

**CBS[1]** (bit 7) is toggled by the update commands themselves: the
application sets it (`0xA4`) for the duration of a bootloader write, and a
latched `0xAF` clears it again afterwards. So `CBS[1] = 0` does not mean "the
bootloader boots and stays there" but "the next power-on enters the bootloader,
which validates the application and chains to it" — which is why a unit
reading `CBS = 01` is a perfectly healthy keyboard running its application.

**The boot chain, concretely.** The silicon has a second, volatile boot-select
bit (`ISPCON.BS`) that is re-loaded as `~CBS[1]` after a power-on — and after
any reset other than a CPU or system reset; a CPU or system reset preserves
it:

- `0xA0` sets `ISPCON.BS = 1` and issues a system reset → the next boot starts
  in the bootloader, and the bootloader stays there.
- A latched `0xAF` issues a **CPU** reset, which preserves `ISPCON.BS = 0` → the
  next boot starts directly in the application.
- A **power-on** re-loads `ISPCON.BS = ~CBS[1]`, so with `CBS = 01` the
  bootloader runs first, validates the application (the same signature and
  checksum check `0xAF` performs) and chains to it.

So the bootloader is on the power-on path of every updated unit, and it
enumerates over USB only if it decides to stay. **Holding `Esc` + `1` + `A`
while plugging the keyboard in forces it to stay in ISP mode**; this is a
boot-time key check in the bootloader, not a RAM mailbox, and it is the only
way to reach the bootloader by hand. (A software reset — the application
clearing the reset-cause flags before its `0xA0` — skips the key check
entirely, which is why the command works without any keys held.)

---

## 5. Firmware file format

Total size must be a multiple of 1024. Both updaters read a **16-byte trailer** at
the end of the file.

### 5.0 The firmware images are obfuscated

The `.bin` images are **obfuscated, not plaintext**. The obfuscation is a fixed,
keyless, byte-wise transform: **invert all bits, then swap the nibbles of each
byte**.

```python
def deobfuscate(b):                     # identical to obfuscate(); involutive
    hi, lo = b >> 4, b & 0xF
    return (((~lo) & 0xF) << 4) | ((~hi) & 0xF)
```

Inversion and nibble-swap are both involutions that commute, so the transform is
its own inverse, is bijective over all 256 byte values, and the order of the two
steps is irrelevant. It is *obfuscation, not encryption* — there is no key and no
entropy, so the plaintext is recoverable offline.

Consequences:

- **The update tool never deobfuscates anything for the device.** It transmits the
  file bytes **verbatim, still obfuscated**. The keyboard applies the transform
  itself, and flash ends up holding the plaintext image: a full readback (§6.6)
  is byte-identical to `deobfuscate()` of the file.
- Therefore every field below is stated in **both** forms. `obf(x)` denotes the
  byte as it appears in the shipped file.
- Only ~2 KB of the 4 KB bootloader is code; the rest is `0xFF` padding. `0xFF` is
  the obfuscation of `0x00`, so the padding is unremarkable.

### 5.1 Validation

```
if size == 0x1000:                       # exactly 4 KB
    require bytes[0xFF4..0xFF8] == 7B 6A 6B CA FA
    → treated as an ISP bootloader image
else:
    require bytes[size-16] == 7B and bytes[size-15] == 6A
    → treated as an application image
```

Failure messages: `"The firmware file is not a valid ISP firmware file."` /
`"The firmware file is not a valid firmware file."`

**These constants are obfuscated ASCII.** Decoding them:

| Constant in file | Deobfuscated | Meaning |
|---|---|---|
| `7B 6A` at `size-16` | `48 59` | `"HY"` — Heng Yu |
| `7B 6A 6B CA FA` at `0xFF4` | `48 59 49 53 50` | `"HYISP"` — Heng Yu ISP |

So the real checks are `file[size-16..size-13] == "HYGF"` (only the first two
bytes, `"HY"`, are tested, plus the profile's own first two bytes) and, for 4 KB
images, `profile[2..6] == "HYISP"`.

The file-type decision is therefore entirely ASCII-driven: a 4 KB image whose
profile says `HYISP` is a bootloader; anything larger is an application.

### 5.2 Trailer layout

The **profile block is 10 bytes at `file[size-14 .. size-5]`**:

```
  offset:  -16  -15  -14  -13  -12  -11  -10   -9   -8   -7  -6  -5   -4..-1
           7B    6A   p0   p1   p2   p3   p4   p5   p6   p7  p8  p9   checksum
           'H'   'Y'  +----------------------- 10-byte profile block ------------+
```

Deobfuscated, the trailer is plain ASCII:

```
  -16  -15  -14  -13  -12  -11  -10   -9   -8   -7   -6   -5   -4 .. -1
   H    Y    G    F    H    Y    I    S    P   0x00 0x00  'U'  <32-bit checksum>
```

| Field | Offset | Value (deobfuscated) | Meaning |
|---|---|---|---|
| magic | `-16`, `-15` | `"HY"` | Heng Yu signature |
| profile id | `-14`, `-13` | `"GF"` | Region/variant — see §5.3 |
| image type | `-12 .. -8` | `"HYISP"` \| `"D4215"` | 5-char identifier: ISP loader, or hardware model |
| reserved | `-7`, `-6` | `0x00 0x00` | padding |
| type flag | `-5` | `'U'` (0x55) \| `0x00` | bootloader \| application |
| checksum | `-4 .. -1` | see §5.4 | little-endian `uint32` |

For a 4 KB ISP image the `HYISP` identifier sits at `0xFF4..0xFF8`, which is exactly
where the `size == 0x1000` branch looks — i.e. **the 4 KB signature check is really
"does the profile declare itself an ISP image"**.

### 5.3 Profile encoding

The 10-byte profile block is sent to the device **raw and still obfuscated**.

```
payload = file[-14:-4]                    # verbatim, no transform
```

The macOS updater sends it this way. The Windows updater applies a per-byte
transform before sending:

```
out[0]      =  swapNibbles(in[0])                    # NOT inverted
out[1..9]   = ~swapNibbles(in[1..9])
where swapNibbles(x) = (x << 4) | (x >> 4)
```

Note the byte-0 asymmetry: `out[0]` omits the inversion while `out[1..9]` include it.
In effect the Windows updater compares byte 0 against a *pre-deobfuscation* value
and bytes 1–9 against *post-deobfuscation* values, i.e. it expects the device to
receive the plaintext profile string.

**Measured result: this transform is wrong.** On hardware, raw bytes are accepted
with `status 0x00` and complete a full update, while every transformed byte string
is refused with `0x18` (§3.1). The device stores and compares the profile in its
raw, obfuscated form, so the Windows transform is a genuine bug in that utility —
one this bootloader exposes by refusing the update rather than mis-detecting the
profile.

> These measurements are from ISP mode and hold for the bootloader's comparison.
> The running *application* compares a different pool a different way (§3.1): it
> accepts the clean §5.0 inverted encoding of its own `HYISP` pool, and the
> plaintext bytes `GF`. What is constant across both stages is that the Windows
> updater's asymmetric transform is not it.

Both updaters decode the *response* by comparing transformed values, which is
where the profile names come from. The names are simply the two-character profile
id in capitals:

| id | name |
|---|---|
| `GF` | Global Full |
| `GL` | Global Lite |
| `CL` | Chinese Lite |

Any other combination produces an empty/unknown profile message. All DK4
application images carry `GF` → **Global Full**, matching what the updater reports
for these keyboards.

> **Practical rule for implementers: send `file[-14:-4]` verbatim.** Do not apply
> the Windows transform. Do not deobfuscate. Require at least 8 bytes.

### 5.4 The trailing 4 bytes are a checksum (read by the device, not the tool)

The trailing 4 bytes are **not** unused padding, and **not** a CRC32. They are the
little-endian `uint32` two's-complement 32-bit sum of the **deobfuscated** image
body:

```python
def checksum(deob_body):                       # body = image minus last 4 bytes
    total = sum(struct.unpack('<%dI' % (len(deob_body) // 4), deob_body)) & 0xFFFFFFFF
    return (0 - total) & 0xFFFFFFFF
```

The checksum field is stored like every other byte: **obfuscated in the file**,
deobfuscated in flash — a full readback (§6.6) returns `1C E1 9D CF` in the
trailing 4 bytes. So to read the field *from a file*, deobfuscate the whole
image, then split. For `L1947V33.bin` the raw trailing bytes `3E E1 26 03`
deobfuscate to `1C E1 9D CF` = `0xCF9DE11C`.

Verified — stored equals calculated for all three images:

| Image | Stored | Calculated |
|---|---|---|
| `A2175V13.bin` | `0x14C953BE` | `0x14C953BE` ✔ |
| `L1947V33.bin` | `0xCF9DE11C` | `0xCF9DE11C` ✔ |
| `L2175V16.bin` | `0xEC8BD38F` | `0xEC8BD38F` ✔ |

Both updaters simply ignore these 4 bytes when transferring, which is correct: they
belong to the payload and are flashed like any other bytes. The *device* validates
them — the bootloader refuses to start an application whose checksum does not
balance, and the application refuses to reboot into the bootloader if the bootloader
image does not balance. This is the anti-brick mechanism, and it is why a
half-written or corrupted image fails safe instead of hanging.

### 5.5 Building the container around a bare image

Third-party DK4 firmware is commonly distributed as a plain ARM Cortex-M binary — no
obfuscation, no padding, no trailer. Such a file cannot be flashed, and each of the
three container requirements is independently fatal:

| Missing | Consequence |
|---|---|
| obfuscation | the device deobfuscates flash in place after programming (§5.0), so plaintext bytes land as garbage |
| whole-KB size | the device advertises a target size and the image must match it exactly (§6 step 4) |
| trailer + checksum | `0xA5` sends `file[-14:-4]` verbatim and has nothing valid to send; and with no balancing checksum the bootloader refuses to boot the result (§5.4) |

The container is reconstructible, and the reconstruction is verifiable rather than
inferred: deobfuscating any vendor image, discarding its trailer, recomputing the
§5.4 checksum, re-appending the trailer and re-obfuscating reproduces the original
file **byte for byte**. Verified on all three images in Appendix A.

The order matters, and one detail is easy to get backwards:

1. pad to the target size with `0xFF` — **before** obfuscation, which is `0x00` in
   the file;
2. append the 16-byte trailer of §5.2, with the application type flag `0x00`;
3. append the little-endian §5.4 checksum, chosen so the `uint32` sum of the
   deobfuscated body is zero;
4. obfuscate the whole image (§5.0), because that is the form the device stores.

Padding with `0x00` instead is **not** equivalent. Erased flash is `0xFF`; a
`0x00`-padded image would be written as `0xFF` and deobfuscate to `0x00`, whereas
the vendor images pad with `0xFF` so the deobfuscated image stays byte-identical to
the file. It also interacts with the write pass, which skips blocks that match the
erased state (§6 step 10).

`hyisp-util convert` performs exactly this and reports the resulting
size, profile and checksum; it touches no hardware. Passing `--convert` to
`flash`, `verify` or `selftest` performs the same reconstruction in memory instead,
which is byte-identical output with nothing written to disk. The flag is
deliberately explicit rather than sniffed: the two forms are not
interchangeable, and guessing wrong means erasing the wrong bytes, so a file that
is not already whole-KB is refused with the recipe as a hint.

---

## 6. Update sequence

Common to both updaters. Where the two differ, both behaviours are given; the
hardware-confirmed one is marked.

1. Open the image, get its length.
2. Validate the signature (§5.1); note whether it is a 4 KB ISP image.
3. Enumerate devices; the selected entry must not be `"ISP Mode"`.
4. Re-validate `fileSize == deviceFlashKB × 1024`, else
   `"Firmware file size is not correct.\nPlease re-plug the keyboard"`.
5. Open the device (`GENERIC_READ|GENERIC_WRITE`, share 3).
6. Send **`0xA0`** to enter ISP mode.
7. Report success and **wait for the device to re-enumerate** as a PID-0
   `"ISP Mode"` / `"HYISP"` device, then re-open the new path. On failure:
   `"Enter ISP mode fail.\nPlease re-plug the keyboard"`.
   Verified live on a DK4: `0xA0` → `status 0x00`, payload `20 00 00` (target
   32 KB, model `0x0000`), and the device re-enumerated as `0x24F0:0x0000` in
   ~1 s. In that mode `0xA6` returns the **bootloader** version `B2175V13`, while
   `0xB0`/`0xA6` in application mode return `S1947V33`. `0xAF` reset then returns
   it to application mode.
8. Read the 10-byte profile from `file[size-14]` and send **`0xA5`** — raw, per
   §5.3. Fail → `"Checking Profile fail"` plus the profile-name message.
9. Send **`0xA4`** to erase. Fail → `"Chip Erase fail"`.
10. **Write pass 1:** seek to `0x100`, build a bitmap of non-empty 256-byte blocks,
    then send **`0xA1`** in 8-byte chunks (Windows: 3 attempts per chunk;
    macOS: 16-byte chunks, no retries). Empty blocks are skipped. Chunks are the
    raw, still-obfuscated file bytes.

    > **"Empty" means all-`0xFF` (erased), not all-zero.** The two image types pad
    > with *opposite* bytes: the 4 KB bootloader has one all-`0xFF` block, while
    > both application images have 46 all-`0x00` blocks. A skip-all-zero test is
    > therefore wrong for the applications — it would skip their padding and leave
    > erased `0xFF` there instead. Skipping against the erased value is the only
    > rule consistent with both image types, and it is what keeps the deobfuscated
    > flash byte-identical to the deobfuscated file, so the §5.4 checksum still
    > balances. The test runs on the **obfuscated** bytes.
11. **Write pass 2** (Windows): seek to `0`, send **`0xA1`** for the first `0x20`
    chunks. → **The vector table is written last** (§7.4), so a failure mid-update
    cannot leave the device unbootable. After 3 failures:
    `"Retried %d times, Update fail at 0x%02X%02X"`.
    The macOS updater instead does a single linear pass, skipping empty 16-byte
    blocks, with no vector-table-last ordering.
12. Send **`0xA8`** to protect. Fail → `"Protect fail"`.
13. Send **`0xAF`** to reset. Return code `0x1B` →
    `"Bootloader upgrade fail, please retry."`; other nonzero → `"Reset fail"`.
14. Success → `"Update successfully."`

Steps 8–14 do not actually require step 6. They are performed by whichever part is
currently executing, and that part is always the one already in ISP mode — so a
keyboard that is *already* in ISP mode can be flashed directly, skipping `0xA0`
and the re-enumeration wait. Verified on a DK4 running `B2175V13`: from ISP mode
`0xA5` accepted the `GFD4215` profile with `status 0x00`, and the full
erase → write → protect → reset sequence completed and returned the unit to
application mode.

What is lost in that path is the target-size confirmation, since the size comes
from the `0xA0` reply (step 4). The device's own checksum validation at reset
(§5.4) is what backstops it: a mis-sized or corrupt image is refused at `0xAF`
and the unit stays in ISP mode rather than booting.

This matters beyond convenience. Third-party firmware can leave a keyboard with no
usable application-mode HID interface at all — such a build exposes the bootloader
when restarted, but nothing to talk to beforehand — so a tool that insists on
starting from application mode cannot recover exactly the units that most need it.

### 6.1 Differences between the two updaters

| | Windows | macOS |
|---|---|---|
| Write chunk size | 8 bytes | 16 bytes |
| Retries per chunk | 3 | none |
| Write ordering | body first, vector table last | single linear pass, skip empty 16-byte blocks |
| Profile transform | swap+invert (**rejected** by `B2175V13`, §5.3) | none (raw — **accepted**) |
| Receive timeout | 5 s | 3 s (300 × 10 ms) |
| HID access | `HidD_SetFeature` / `HidD_GetFeature` | `IOHIDDeviceSetReport/GetReport` |

### 6.2 Verifying the sequence on real hardware

`hyisp-util verify [firmware]` exercises every non-writing part of the protocol
against a connected keyboard and reports a pass/fail table, so two units can be
diffed. It never erases, writes, or protects.

```console
$ hyisp-util verify L1947V33.bin
PASS: 16/16 checks passed
application version: 'S1947V33'
bootloader version: 'B2175V13'
```

It covers discovery, `0xB0`, `0xA0` entry plus its target size and model,
re-enumeration, `0xA6`, `0xA5`, `0xAF` reset, and — unless `--no-latch` is passed
— deliberately triggers the `0xAA` latch and then proves the tool recovers from it.
That last part is the reason the command exists: recovery is only known to work
because something exercises it.

The `0xA5` check inside `verify` asserts **`status 0x00`**, not merely "answered": a
`0x18` there means the next real `0xA4` will fail too (§3.1), so `verify` must not
report success for a unit that cannot be updated.

`verify` writes no flash, but it is not entirely read-only: it enters ISP mode and,
unless `--no-latch` is given, exercises the `0xAA` latch and recovers via `0xAF`.

### 6.3 A complete update, executed on hardware

Recorded end to end on a DK4 (`B2175V13` / `S1947V33`), flashing `L1947V33.bin`
onto a unit already running that same version:

```console
$ hyisp-util flash L1947V33.bin --yes
L1947V33.bin: application, 32 KB, profile Global Full
found 0x24F0:0x0140 Das Keyboard
  0xA0 enter ISP
  device reports target 32 KB, model 0x0000
  waiting for the keyboard to re-enumerate in ISP mode...
  re-enumerated: 0x24F0:0x0000 HYISP
  0xA5 check profile
  profile accepted: unknown
  0xA4 erase chip
  pass 1: wrote 127 block(s) from 0x0100, skipped 0 empty
  pass 2: wrote 0x0000-0x1FFF (vector table last)
  0xA8 protect chip
  0xAF reset

Update successfully.
```

A `verify --no-latch` run immediately afterwards passed 12/12 and the keyboard
answered `0xB0 → 'S1947V33'`. `profile accepted: unknown` is expected: `0xA5` returns
a bare status byte with no echo, so the profile name is read from the image file,
never from the device.

The same sequence was then run with `L2175V16.bin`, also `Update successfully.`,
also followed by a clean `verify` (16/16). That second update is what surfaced the
variant-dependent PID: the unit came back as `0x24F0:0x0141` instead of `0x0140`,
and `0xAA` began answering `04 01 41` instead of `04 01 40` (§1). Both images carry
the identical `GFD4215` profile, so `0xA5` accepted both.

The first attempt at this same sequence failed at `0xA4 erase chip` with
`status 0x18`, because the profile had been sent nibble-swapped/inverted. The
existing firmware was untouched and the unit was recovered from ISP mode with a
plain `0xAF` — the failure mode is safe, but it is silent rather than obvious, so
it is worth recognising `0xA4` + `0x18` as "the profile check is the real
complaint".

Two host-side details matter when writing any tool against this device, both found
by running it:

- **Enumerating is not the same as usable.** After a reset or mode change the DK4
  can stay absent from HID enumeration for up to ~18 s, and its entry can briefly
  exist while the handle still fails on first use with `OSError: read error`. Poll
  for the device *and* confirm it answers a command before relying on it; wrap
  `OSError` from hidapi as a disconnect, since it signals re-enumeration rather
  than a real fault.
- **Recovery needs no physical access.** `0xAF` alone clears the `0xAA` latch, so
  the tool does this itself instead of telling the user to unplug.

`flash --dry-run` still enters ISP mode and skips `0xAF`, so it can leave the
keyboard in ISP mode; recover with `reset_to_app_mode()`.

### 6.4 A full round trip through third-party firmware

Also recorded on a DK4, and the reason §5.5 and the note above step 14 exist.

A bare third-party build (22 448 bytes, plain, unpadded, no trailer) was converted
per §5.5 and flashed from application mode: `Update successfully.`. The unit then
enumerated as `0x24F0:0x0140` with product string `das_keyboard/4_professional`
instead of `Das Keyboard`, and — as expected for firmware that does not implement
the vendor command interface — its vendor HID collection could no longer be opened,
so `0xB0`, `0xA6`, `verify` and a further `flash` all became unreachable. The
application had to be restored through the bootloader.

Restarting that build into the bootloader brought the device back as
`0x24F0:0x0000` `HYISP` with the vendor collection present, and `0xA6` answered
`B2175V13` — the bootloader had survived the application update untouched, which
also confirms the bootloader lives outside the 32 KB application region. Flashing
`L1947V33.bin` from that state, skipping `0xA0`, returned `Update successfully.`,
and the unit came back as `Das Keyboard` answering `0xB0 → 'S1947V33'`, with
`verify --no-latch` passing 12/12 — identical to its pre-update state.

Two observations worth keeping:

- The bootloader is untouched by an application update, so an unflashable
  application is recoverable as long as ISP mode can be reached. That is the
  safety property that makes third-party firmware practical on this hardware.
- The conversion in §5.5 is not merely plausible — it has now produced an image
  the device booted, and the vendor image written back over it verified clean.

What was written to flash above came from the `convert` → `flash` route. The
`--convert` route, which reconstructs the same image in memory and writes nothing
to disk, has since been run end to end on the same unit and behaved identically
through the whole loop: `0xA5` accepted, erase and write completed, the unit came
back as `das_keyboard/4_professional` with no command interface, and the official
Windows image written over it from ISP mode returned the unit to `S1947V33` with
`verify --no-latch` clean. It also reproduces the flashed file byte for byte. So
the two routes are interchangeable on hardware, not merely equivalent offline.

### 6.5 Flashing the bootloader

**Implemented, and hardware-validated in both directions.** The bootloader (4 KB
LDROM) is updated through a second copy of the flash command set that the
*running application* implements, whose addresses target the LDROM window at
`0x100000 + offset`. From ISP mode the same opcodes address the application
window instead, which is why the sequence below can only run from application
mode — and why the bootloader can never write itself: whichever stage is
executing, it writes only the *other* stage's region. (The mutual dependency is
the same insight the vendor tools never exercise: an application cannot write
itself, so the application is what pushes a new bootloader; the bootloader is
what writes a new application.)

The full sequence, measured on a DK4 running `B2175V13` + `S1947V33` and
executed end to end in both directions — the original bootloader replaced with
the macOS-package image `A2175V13.bin` (the unit re-enumerated with PID
`0x0001`, §1), then restored from a plaintext dump of the original:

| step | command | payload | result |
|---|---|---|---|
| 1 | `0xA5` | `GF`, plaintext, 2 bytes | `0x00` — the application's profile pool is the handshake string, not the firmware trailer (§3.1) |
| 2 | `0xA4` | — | `0x00` — erases the LDROM window |
| 3 | `0xA1` × N | `addrHi addrLo data…` | offset from the LDROM base; data goes on the wire as it appears in the image file, the device deobfuscates it (§5.0). Measured with 64-byte chunks; the application's frame parser accepts larger counts than the bootloader's |
| 4 | `0xAA` | — | `0x00`, echo `04 01 <appPID>` — latches: from here `0xAF` verifies before resetting (§3.1) |
| 5 | `0xAF` | — | `0x00`: the device confirmed `HYISP` at `0x100FF4` and a zero uint32 word sum over all 4 KB, then resets into the new bootloader. `0x1B`: verification failed, and **no reset happens** — the keyboard stays in application mode with the LDROM erased, and the write can be retried |

Notes:

- **There is no file-size guard on this route; step 5's verify is the only
  gate.** Validate the image offline before starting: `HYISP` at offset `0xFF4`
  and a balancing checksum (§5.4). An unbalanced image erases the LDROM and is
  then rejected at `0xAF`, which is recoverable but leaves the keyboard without
  a bootloader until a good image is written.
- Step 4 is what makes step 5 meaningful. Sent bare — with no completed write
  sequence in front of it — `0xAA` still latches the device and breaks `0xA0`
  until `0xAF` (§3.1).
- The new bootloader is entered afterwards with the ordinary `0xA0`; like every
  image it enumerates with its own PID (§1), so discovery needs no changes.
- The vendor updaters never reach this route. They gate on the size byte of
  `0xA0`'s response (`fileSize == responseByte0 << 10`, §4.1), which reports 32
  for the application window, so a 4 KB file fails their guard before any of
  the above runs; and where they do select `0xAA` for a 4 KB image, they send
  it bare, where it only latches.
- The LDROM sits outside the `0xA7` read window (§6.6), so this write path and
  the `0xAF` verify are the only tools for it; there is no read-back
  confirmation beyond the device's own gate.
- `hyisp-util flash-bootloader` implements exactly this sequence. It accepts
  a vendor bootloader container (sent verbatim) or a plaintext dump read back
  from flash (obfuscated in memory), pre-validates the signature and checksum
  before erasing anything, and on success enters the new bootloader once via
  `0xA0` to confirm it boots before returning the keyboard to application mode.

### 6.6 Reading the image back (`0xA7`)

Neither vendor updater ever transmits it, but the bootloader implements a
read-only flash command, measured on live hardware:

```text
request:  EA 05 A7 <addrHi> <addrLo> <count> <XOR>
response: ED <len> A7 00 <byte0> … <byteN-1> <XOR>        len = count + 3
```

- The address is **16-bit big-endian**, the same convention `0xA1` uses.
- `count` bytes come back with status `0x00`. The command only reads: it never
  writes, erases or resets anything, so it is safe at any time.
- **ISP mode only.** From application mode `0xA7` answers `0xF1`, like every
  opcode the running application does not implement.
- Reads past the image are not refused — address `0x8000` returns the bytes that
  follow with status `0x00` — so stay inside the advertised region.

**Response size vs the feature-report channel (§2).** A frame is `count + 5`
bytes, and a single read delivers at most 15 data bytes, so:

- `count ≤ 10` → the whole reply fits in one read;
- `count > 10` → the reply spans successive reads, and a read can catch the
  buffer half-filled, which fails the XOR checksum. The command is idempotent:
  resend until the checksum validates.

`hyisp-util read` uses `count = 10` and retries checksum failures.

Measured on hardware: the full 32 KB application image reads back **byte-identical
to `deobfuscate()` of the shipped file** (§5.0), checksum field and trailer
included. Flash therefore holds the plaintext image, and the transform is the
keyboard's own doing — the file on disk stays obfuscated throughout.

The window is 16-bit, so it covers only the 32 KB application region. The 4 KB
bootloader image (§6.5) sits outside it and cannot be read with `0xA7`.

---

## 7. Firmware samples

Three images were examined. Their classification is self-describing: each carries its
own identifier inside the file, and the 5-character trailer identifier names the
model.

| File | Size | KB | Classification | Evidence |
|---|---|---|---|---|
| `A2175V13.bin` | 4 096 | 4 | **ISP bootloader** | `size == 0x1000`; trailer `HYISP`; self-ID `B2175V13` |
| `L1947V33.bin` | 32 768 | 32 | application | trailer `D4215`; self-ID `S1947V33` |
| `L2175V16.bin` | 32 768 | 32 | application | trailer `D4215`; self-ID `S2175V16` |

All three are **Das Keyboard 4** (`D4215`, VID `0x24F0` / PID `0x0140`) firmware.

The shipped filename is the in-firmware identifier with a one-letter role prefix:
the bootloader contains the literal `B2175V13` and the applications contain
`S1947V33` / `S2175V16`, shipped as `A…` and `L…` respectively. The images agree
with their filenames, but the classification does not depend on them — do not infer
a role from a filename prefix alone.

The 32 KB application size means the target keyboard must answer `0x00` (32) in its
`0xA0` response.

### 7.1 Observed trailers

Raw, as shipped (still obfuscated), and deobfuscated:

```
raw            -16  -15  -14  -13  -12  -11  -10   -9   -8   -7   -6   -5   -4 .. -1
A2175V13       7B   6A   8B   9B   7B   6A   6B   CA   FA   FF   FF   AA   14 CA 63 BE
L1947V33       7B   6A   8B   9B   BB   BC   DC   EC   AC   FF   FF   FF   3E E1 26 03
L2175V16       7B   6A   8B   9B   BB   BC   DC   EC   AC   FF   FF   FF   07 C2 47 31

deobfuscated    H    Y    G    F    H    Y    I    S    P   00   00    U   BE 53 C9 14
L1947V33        H    Y    G    F    D    4    2    1    5   00   00   00   1C E1 9D CF
L2175V16        H    Y    G    F    D    4    2    1    5   00   00   00   07 C4 8B EC
```

Deobfuscated, every trailer is `"HYGF"` + a 5-character identifier + `0x00 0x00` +
a type flag + a little-endian checksum. All three decode to `GF` → **Global Full**.

### 7.2 Profile semantics

| Field | Bootloader (`A`) | Application (`L`) | Interpretation |
|---|---|---|---|
| `-16`,`-15` | `"HY"` | `"HY"` | Heng Yu signature |
| `p0`,`p1` | `"GF"` | `"GF"` | Region/variant — "Global Full" (§5.3) |
| `p2`–`p6` | `"HYISP"` | `"D4215"` | 5-character image identifier: ISP loader, or hardware model |
| `p7`,`p8` | `0x00 0x00` | `0x00 0x00` | padding |
| `p9` | `'U'` (0x55) | `0x00` | **Image type**: bootloader \| application |

`p9` is a second, independent type marker, so the firmware can discriminate
bootloader from application even when the `0xA5` response is a bare status byte.

### 7.3 Image properties

- **Fully dense.** Both 32 KB images are programmed to the last byte. Because `0xFF`
  is the obfuscation of `0x00`, the padding is ordinary erased flash.
- **The Windows block-skip is effectively inert.** It marks a 256-byte block "empty"
  only if every byte is `0x00`. With `0xFF` padding almost no block qualifies, so
  nearly the whole image is still transmitted. Harmless, but the optimisation does
  not do what it appears to.
- **Two independent app builds.** `L1947V33` vs `L2175V16` differ in 19 182 of
  32 768 bytes (58.5 %) across 876 separate regions — a full recompile with
  code-layout shift, not an incremental patch. Their trailers are identical apart
  from the checksum, so they target the same model and the same profile. The
  keycode mapping is therefore not a small table that was tweaked between builds.

### 7.4 The deobfuscation is verified by the image header

Deobfuscating produces a **valid ARM Cortex-M vector table** at offset 0, which is
independent confirmation that §5.0 is the correct transform (the raw files contain
no plausible stack pointer or handler address at all):

| Image | Initial SP | Reset vector | First handlers |
|---|---|---|---|
| `A2175V13` | `0x20000600` | `0x000000D5` | `0xD9`, `0xDB` |
| `L1947V33` | `0x20000CA8` | `0x000000D5` | `0xF5`, `0xF7` |
| `L2175V16` | `0x20000E90` | `0x000000D5` | `0xF5`, `0xF7` |

The stack pointers all sit inside a small `0x20000000` SRAM aperture and every vector
has the Thumb bit set, identifying the part as a **Nuvoton NUC123** (Cortex-M0),
consistent with the `HY` / `HYGF` / `HYISP` markers.

This also explains the Windows updater's "write the first 256 bytes last" behaviour
(§6 step 11): that region is the vector table, so it must be committed only once the
rest of the image is safely in flash.

---

## 8. Open questions

1. **Opcodes `0xB1` and `0xD0`** — *resolved.* In ISP mode both answer `0xF1`,
   the same status as every opcode the bootloader does not implement (§4.1). In
   application mode `0xD0` also answers `0xF1`, while `0xB1` returns `D4215` —
   the running application's own identifier (§5.2), not a bootloader version;
   `0xA6` already is the bootloader-version read.
2. **The `0x00` test-mode frame** — *resolved.* It fails the device's XOR check
   (`XOR(EA,03,00,1F) = 0xF6`, but the trailer says `0x18`), so the device
   answers `0xFC` and never dispatches it; and even with a valid checksum,
   `0x00` is implemented by neither stage and would answer `0xF1`. The command
   has no effect on either image. See §4.4.
3. **Where the deobfuscation happens** — *resolved by §6.6.* Flash stores the
   plaintext image: a full readback is byte-identical to `deobfuscate()` of the
   file, checksum field included. The transform is applied by the keyboard
   itself; the host tools play no part in it. (§5.4's reset-time validation
   consequently runs over the plaintext image.)

---

## Appendix A — Firmware images and file naming

| File | Size | KB | Classification | Evidence |
|---|---|---|---|---|
| `A2175V13.bin` | 4 096 | 4 | **ISP bootloader** | `size == 0x1000`; trailer identifier `HYISP`; self-ID string `B2175V13` |
| `L1947V33.bin` | 32 768 | 32 | application | trailer identifier `D4215`; self-ID string `S1947V33` |
| `L2175V16.bin` | 32 768 | 32 | application | trailer identifier `D4215`; self-ID string `S2175V16` |

Deobfuscated copies of all three can be produced locally with `deobfuscate()` from
§5.0; they are generated output, not checked in. A verification harness in
`hyisp-util selftest` re-derives every field in §5.2 and §5.4 from the raw `.bin`
files and passes on all three.

Provenance of the two application images, since the assignment of OS variant rests
on it rather than on the file contents: `L1947V33.bin` ships in the archive
containing the vendor's **Windows** flashing utility, `L2175V16.bin` in the archive
containing the **macOS** flashing utility.
