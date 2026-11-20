# Serial protocol (115200 8N1, USB CDC)

The host client (`host/ga100vfi/glitch.py`) speaks this over `/dev/ttyACM0`.
Commands are newline-terminated ASCII; responses are line-oriented.

## Glitch

```
g <v_dip_mv> <t_dip_us> <v_restore_mv> <t_restore_us> <t_revert_us> <fast> <hold_ms> <pulses> <pw_us> <gap_us> <delay_us> <revert>
```

All arguments after `v_dip_mv` are optional and fall back to firmware defaults
(`-100 650 -20 0 1 0 1 0 0 75 1`). `pw`/`gap` ≤ 0 are auto-sized, then floored at
one PMBus write (~36 µs at 1 MHz) so steps cannot collide. `delay_us` advances the
dip edges to compensate the rail's response lag. `revert` (default 1) hands the rail
back to PWM-VID mode 500 ms after the dip; set `revert=0` to skip that wait and leave
the rail in PMBus mode at the stock voltage — much faster for campaigns (the GA100
runs fine on the fixed voltage; call `w` to hand back at the end).

Lab knobs (since `fw=lab-psr1-push1`; fast subsets, dither and SCL hold were run on
one card in 2026-11). With
`fast` 0 or 1, `t_restore` above -1000, `revert` 0 or 1 and `hold_ms` 0 the timeline
is the same as before:

| Field | Value | Effect |
|---|---|---|
| `fast` | 0 / 1 | stock slew / whole fast profile, as before |
| `fast` | 2..7 | one subset of the fast profile: 2 = `0x30` + `0x32`, 3 = `0x30`, 4 = `0x32`, 5 = `0xBA`, 6 = `0xB9`, 7 = `0x45` (`0x31` only with 1). The card's six values are still captured before and restored after. Other values abort: `glitch ABORT: fast=<n>, use 0..7` |
| `t_restore_us` | ≤ -1000 | longer `PEX_RST` lead: reset and the 600 mV setup write move from -1000 to `t_restore` (setup writes at `t_restore` - 300 / - 200), so a dip that starts before -1000 is not overwritten. `t_restore` then counts as -20 for an automatic `pw`. Below -20000 aborts: `glitch ABORT: reset lead <n> us, use -20000..-1000` |
| `revert` | 2 | one-VID dither: odd pulses are one VID code (6.25 mV) shallower than `v_dip`, with no restore and no gap between pulses, so the command steps between the two codes every `pw`; `v_restore` follows the last pulse. No hand-back (as `revert=0`). Since push2 the dither `pw` is floored at 47 µs (one measured write) |
| `hold_ms` | > 0 | if the train's last write is scheduled before t = 0, SCL is held low from t = 0 for `hold_ms`; release waits up to 200 µs for SCL to read high (a failure stops the shot), and the stock restore (`VOUT` `0x7E`, retried for up to 500 µs, then `VOUT_MIN` `0x30`) runs 200 µs after the end of the hold, so the rail stays at `v_restore` meanwhile. A train that writes after t = 0 keeps the old hold (after the stock restore): the echo says `hold_at=post` and a `glitch WARN: hold_at=post ...` line follows. The profile restore waits 200 µs after any hold |

The 36 µs `pw`/`gap` floor stays; the echo below prints the resolved `pw` and `gap`.

Refused before anything is written (since push2, `fw=lab-psr1-push2`):
a VOUT step before the 600 mV setup write (`glitch ABORT: VOUT step at <t> us is
overwritten by the setup write at <reset_at> us`), and any PMBus write in flight across
t = 0, counting ~47 µs per write and the queueing of back-to-back writes (`glitch ABORT:
PMBus write act=<n> scheduled at <t> us runs from <start> us across the PEX_RST release at
0`). One card's partial-state shape (`td` +30, `pw` 36, `delay` 117) is refused this way:
its restore write queues to -40 µs and runs past the release, which the old step loop
measured as a +9 µs late release. A tainted or failed profile capture, a failed profile
write (except the full profile's `0x31` write, which this controller refuses) and a
priming alarm that did not fire also abort, after restoring any profile already written.

Reaching the Goldilocks fuse state (DSOV=0, CRC_STATUS=0x55, PLMs=0xffffffff) needs a
**three-pulse** waveform (`pulses=3`) at a **shallow** dip: a single dip, or a deep
triple pulse (≈335 mV), only reaches the over-glitched CRC=0x00 state (whole block
sensed as zero). A shallow triple pulse (~354 mV) disrupts only the row0/1 control
fuses, leaving the CRC valid. A fast campaign unlocks once (`u`, which also writes
config-id `0x1229` so the GA100's devinit stops re-locking the VRM), then issues the
triple pulse with no SCL hold (`hold_ms=0`) and no hand-back (`revert=0`):

```
u
g 354 -60 750 -20 0 1 0 3 198 150 117 0
```

Verified on hardware (2026-11-13): 14 % clean Goldilocks per attempt (100/100 PCIe
link-up, 0 NACKs) at ~5.5 attempts/s (`~181 ms`/shot). `hold_ms`>0 (SCL hold) is
**not** needed once config-id is set and can wedge the GPU's boot — leave it 0.

On entry the firmware echoes the resolved parameters:

```
glitch vd=<n> td=<n> vr=<n> tr=<n> tv=<n> fast=<n> hold=<n> pw=<n> gap=<n> delay=<n> revert=<n> reset_at=<n> dither=<0|1> hold_at=<none|release|post>
```

Since push2 the echo and every line below are printed after the shot, so no USB
traffic is queued around t = 0. Two PMBus writes scheduled less than one write apart
are reported (the second one runs late):

```
glitch WARN: PMBus writes at <t1> and <t2> us are <d> us apart (one write ~47 us); the second runs late
```

`PEX_RST` release at t = 0 is not a timeline step: a hardware alarm releases the pin
at the scheduled time even while a PMBus write is in flight. Its handler is installed
directly on the alarm IRQ (highest priority, core 0) and runs from SRAM with no flash
code on the path; core 1 busy-waits instead of using the SDK alarm pool. The alarm is
armed by the assert step itself, after the pin is low, so it can never release a card
that was not put in reset; an assert that starts more than one write late fails the
shot. Before every shot a throwaway alarm (pin already high-Z) exercises the IRQ path;
how late it ran is printed as `prime_at=<us>` (with push1 on one bench on 2026-11-20 the
first release of every boot was +9..+10 µs, later shots 0..2 µs; with push2 the first
release of three fresh boots read 0).

The timeline stops at its first failed action. The firmware then writes the stock rail
(`VOUT` `0x7E`, `VOUT_MIN` `0x30`) while the card is still held in reset if the release
has not happened yet, releases `PEX_RST`, restores the profile and reports:

```
glitch FAIL: step i=<n> act=<n> scheduled=<t> failed; timeline stopped, safe rail vout=<0|1> vmin=<0|1>, PEX_RST released
glitch FAIL: VRM profile restore or readback failed; power-cycle the card
```

The profile restore writes back only the registers the `fast` value touched and reads
them back against the capture.

The timing diagnostic emits one record per action after the waveform and profile
restoration, before the completion response:

```
timing i=<n> scheduled=<signed_us> act=<n> arg=<n> start=<signed_us> end=<signed_us> ok=<0|1>
```

Records are printed for the actions that ran. Act 9 is the stock VOUT write after a
hold from the release, retried for up to 500 µs. Times are relative to the scheduler
reference. Reset assertion is act 3. These
measure software call timing, not physical rail or reset thresholds. Timestamp
overhead perturbs timing. See [TIMING_DIAGNOSTIC.md](TIMING_DIAGNOSTIC.md).
After the records, the priming alarm and then the release: the time the release alarm
ran (`via=alarm`), the release after a failed action (`via=abort`), or the release after
the timeline if the alarm did not run (`via=fallback`; not expected):

```
prime_at=<us late>
release_at=<signed_us> scheduled=0 via=<alarm|abort|fallback>
```

After the profile restoration and the 30 ms settle the firmware reads READ_VOUT
(`0x8B`). If it reads below 500 (the runner's CONTROLLER OFF threshold) or fails, it
reads STATUS_VOUT, _IOUT, _INPUT, _TEMPERATURE, _OTHER, _MFR_SPECIFIC and STATUS_BYTE
(standard PMBus names; the MP2888B mapping is unconfirmed) before the CML read and
prints them (`ERR` for a byte that does not read). A healthy shot prints nothing here:

```
rail_code=<n|ERR> status 7a=<hh> 7b=<hh> 7c=<hh> 7d=<hh> 7f=<hh> 80=<hh> 78=<hh>
```

Response (the host waits for the complete newline-terminated `glitch done` line
before reading BAR0):

```
glitch done pwd=<0|1> e1=0x<hhhh> nvvdd=<v.vvvv> writes_failed=<n>
```

`pwd` is the VRM unlock state, `e1` the MP2888 status word, `writes_failed` the
count of failed actions: the timeline action that stopped the shot, failed safe-rail
writes and a failed profile restore or readback (0 = everything ran). `nvvdd`
is the captured-rail average and is **present only in the debug build**
(`-DDEBUG=ON`); the normal build omits it.

## VRM unlock / status

| Cmd | Meaning |
|---|---|
| `u` | Unlock the MP2888B (password `0x7C25`) |
| `z` | Lock status → `cml=0x<hh> pwd=<0|1> e1=0x<hh><hh> vout=0x<hh><hh>` |
| `v` | Status → debug build: `clk=<n> MHz  nvvdd=<v.vvvv> V  sample_ns=<f>  last reset: <…> fw=lab-psr1-push2-debug`; normal build: `clk=<n> MHz  last reset: <…> fw=lab-psr1-push2` |
| `y` | NVVDD trace → header `trace n=<n> sample_ns=<f>` then `<n>` hex samples; `trace: built without DEBUG` in the normal build |
| `w` | Revert |
| `k` / `K` | Apply `fast` / `stock` MP2888B transient-response profile |
| `b` | Reboot RP2350 into USB BOOTSEL mode |

`MATCH` in the `u` response means the password was accepted.

Every `u` then writes reg `0x13` = `0x00` and config ID `0x46` = `0x1229`, reads both
back and prints `unlock <MATCH|LOCKED|ERR> wp=<hh|ERR> cfg=<hhhh|ERR>[ write=ERR]`
(`unlock MATCH wp=00 cfg=1229` on a healthy controller). Before, both writes were
skipped when the controller already reported MATCH. Since push2 every `u` also forgets
the VRM profile captured for fast shots, so a capture never carries over to another card.

## PMBus over the bridge

```
@s                       # scan 0x08..0x77 for ACKing 7-bit addresses
@r <addr> <reg> <n>      # repeated-START read of n bytes (hex)
@w <addr> <b0> <b1> ...  # write bytes, STOP (hex)
@q                       # release / float I2C pins
@F <hz>                  # set I2C baud (100000, 400000, or 1000000)
@L / @U                  # hold SCL low / release SCL to high-Z
```

## Reset

| Cmd | Meaning |
|---|---|
| `r` | PEX_RST pulse (100 ms low) — the fuse re-sense trigger |
| `j` / `J` | Hold / release `PEX_RST_BUF*` |

## Safety invariants enforced in firmware

- `PEX_RST` is only ever driven **low**, then released to high-Z (the net is 1.8 V
  push-pull; driving it high would put 3.3 V on it).
- The release alarm is armed only after `PEX_RST` is low, and every glitch timeline
  ends with the alarm cancelled and `PEX_RST` released, so a shot can neither release
  an unreset card on schedule nor leave the card held in reset.
- A failed timeline action stops the shot: the stock rail is written (while the card
  is still in reset when possible) before `PEX_RST` is released.
- After the dip the firmware restores `VOUT_COMMAND` to the stock code and hands the
  rail back to PWM-VID mode, so an interrupted run cannot leave NVVDD low.
- The NVVDD ADC is read-only and never gates actuation, so the sense wire
  (`PIN_NVVDD_ADC` in `firmware/board.h`: GP42 on RP2350B, GP26 on RP2350A) is
  optional. The ADC exists only in the debug build (`-DDEBUG=ON`); the normal build
  never touches the ADC pin — just leave it unwired.
