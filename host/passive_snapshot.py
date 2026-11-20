"""Read the diagnostic registers without writes, stopping at the first bad read.

Stops at a 0xBADF or 0xBAD0 value, an I/O error, an all-ones response (PLM excepted), a FUSECTRL other than the idle
0xE0040000, a GFW boot status whose low byte is not 0xFF (still booting), or a DSOV other than 0 or 1. The same rules
are copied into the per-boot runner of the cmp170hx-8gb-vfi-tools repository (tools/runner/bisect_shots.py)."""
from __future__ import annotations

import argparse
import json
import mmap
import os
import re
import struct
from pathlib import Path
from typing import Callable

REGISTERS = (
    ("FUSECTRL", 0x820000),
    ("GFW", 0x118234),
    ("DSOV", 0x820084),
    ("CRC", 0x820070),
    ("PLM", 0x8200FC),
)


def observe(read32: Callable[[int], int]) -> dict:
    result = {"state": "unknown", "reason": None,
              "registers": {name: None for name, _ in REGISTERS}}
    values = result["registers"]
    for name, address in REGISTERS:
        try:
            value = read32(address)
        except OSError as exc:
            result["reason"] = f"{name}: {exc}"
            return result
        values[name] = value
        if value & 0xFFFF0000 == 0xBADF0000:
            result.update(state="poisoned", reason=f"{name}: BADF sentinel")
            return result
        if value >> 20 == 0xBAD:
            result.update(state="poisoned", reason=f"{name}: BAD0 error read")
            return result
        if value == 0xFFFFFFFF and name != "PLM":
            result["reason"] = f"{name}: all-ones response"
            return result
        if name == "FUSECTRL" and value != 0xE0040000:
            result["reason"] = f"FUSECTRL: unexpected non-idle value 0x{value:08x}"
            return result
        if name == "GFW" and value & 0xFF != 0xFF:
            result.update(state="booting", reason=f"GFW low byte is 0x{value & 0xFF:02x}, not 0xff")
            return result
        if name == "DSOV" and value not in (0, 1):
            result["reason"] = "DSOV: unexpected value"
            return result
    if values["DSOV"] == 1 and values["CRC"] == 0x55 and values["PLM"] == 0xFFFFFFFF:
        result["state"] = "healthy_locked"
    elif values["DSOV"] == 0 and values["CRC"] == 0x55 and values["PLM"] == 0xFFFFFFFF:
        result.update(state="candidate", reason="Passive signature only; active validation not performed")
    else:
        result.update(state="partial_or_corrupt", reason="Diagnostic signature is not healthy")
    return result


def snapshot(bdf: str) -> dict:
    if not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", bdf):
        raise ValueError("Expected a PCI BDF such as 0000:05:00.0")
    device = Path("/sys/bus/pci/devices") / bdf
    if (device / "driver").resolve(strict=True).name != "pci-stub":
        raise RuntimeError("GPU must be bound to pci-stub")
    with (device / "config").open("rb") as stream:
        config = stream.read(20)
    if len(config) != 20 or struct.unpack_from("<I", config)[0] != 0x20C210DE:
        raise RuntimeError("CMP 170HX PCI identity unavailable or changed")
    if not struct.unpack_from("<H", config, 4)[0] & 2:
        raise RuntimeError("PCI memory decode is off; observer will not enable it")
    with (device / "resource").open() as stream:
        start, end, _ = (int(x, 16) for x in stream.readline().split())
    if not start or end - start + 1 < 16 << 20:
        raise RuntimeError("BAR0 allocation is absent or too small")
    if struct.unpack_from("<I", config, 16)[0] & 0xFFFFFFF0 != start & 0xFFFFFFF0:
        raise RuntimeError("BAR0 is not restored; observer will not restore it")
    fd = os.open(device / "resource0", os.O_RDONLY | os.O_SYNC)
    try:
        with mmap.mmap(fd, 16 << 20, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ) as bar:
            result = observe(lambda offset: struct.unpack_from("<I", bar, offset)[0])
    finally:
        os.close(fd)
    result["bdf"] = bdf
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bdf", default=os.environ.get("VFI_BDF"), required=not os.environ.get("VFI_BDF"),
                        help="the card's PCI address (lspci -Dn -d 10de:20c2), or set VFI_BDF")
    args = parser.parse_args()
    try:
        result = snapshot(args.bdf)
    except (OSError, ValueError, RuntimeError) as exc:
        result = {"state": "unknown", "reason": str(exc), "bdf": args.bdf}
    print(json.dumps(result, indent=2))
    return 0 if result["state"] in ("healthy_locked", "candidate") else 2


if __name__ == "__main__":
    raise SystemExit(main())
