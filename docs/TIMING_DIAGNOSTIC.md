# Timing telemetry

This build records when each step of a glitch timeline really ran. `v` reports it as `fw=lab-psr1-timing1`
(normal build) or `fw=lab-psr1-timing1-debug`.

Each step records the start and end of its call and whether it succeeded, in RAM. After the waveform and the
profile restore, and before the `glitch done` line, the firmware prints one line per step:

```
timing i=0 scheduled=-1300 act=8 arg=32 start=-1300 end=-1264 ok=1
```

The line above shows the format, not a measured result. Times are signed microseconds relative to the scheduler's
reference (t = 0 is the scheduled `PEX_RST` release). Act 1 writes `VOUT_COMMAND`, act 3 asserts `PEX_RST` low and
act 4 releases it to input. These are software call times: they do not measure the physical reset edge or the rail.
Taking the timestamps adds a little time to every step; nothing is printed inside the loop. Normal and debug builds
carry the same instrumentation, outside the ADC code.

## What it showed on one bench (2026-11-20)

Shots at a harmless 700 mV (the GPU idles near 695 mV), `delay` 117 µs, on one 8 GB card:

- One PMBus write takes about 47 µs (`PMBUS_W_US` assumes 36).
- The step loop blocks on every write. When a `VOUT_COMMAND` write is still running at t = 0, the release waits for
  it:

| Shot | `t_dip` | `PEX_RST` released at |
|---|---|---|
| single 36 µs pulse | +81 µs | +59 µs |
| single 36 µs pulse | +82 µs | +13 µs |
| single 36 µs pulse | +100 µs | +30 µs |
| standard 3 x 198 µs train | -85 µs | +43 µs |
| slow single 36 µs dip (`fast` 0), recovery 825 mV | +30 µs | +9 µs |
| standard 3 x 198 µs train | -60 µs (default) | on time |

A timing map whose shots put a write across t = 0 moves the release along with `t_dip`. Check where each write
falls before reading meaning into a timing edge. Pulse widths below 36 µs are raised to 36 µs by the firmware, so
`pw` 25, 27, 30 and 35 all send the same pulse.

Keep the NVVDD ADC lead off the card for these comparisons unless you need the trace: on the lab's bench an
attached lead made one card crash 30 to 70 mV earlier than without it.

## Passive snapshot

`host/passive_snapshot.py --bdf <bdf>` (or `VFI_BDF`) opens BAR0 read-only and never touches PCI settings. It
requires the `10de:20c2` identity, `pci-stub`, memory decode on and BAR0 allocated, and stops otherwise. It reads
FUSECTRL first, then GFW, DSOV, CRC and the privilege mask. A `0xBADFxxxx` value at any position stops the reads;
unread fields stay `null`. GFW still booting (low byte `0x01`), DSOV other than 0 or 1, I/O errors and unexpected
all-ones reads also stop it. A privilege mask of `0xFFFFFFFF` is a valid value. It never writes DSOV_SET and never
runs a fuse sense. `candidate` means only that the passive signature (DSOV 0, CRC `0x55`, mask `0xFFFFFFFF`)
matches; it validates nothing.

## Serial replies

`Card.cmd(until=...)` now waits for the newline that ends the completion line. Before, it stopped at the first
`glitch done`, and a USB packet that split the reply left the fields unparsed: the shot was logged as a NACK and
went unclassified.
