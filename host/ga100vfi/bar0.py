"""BAR0 access and post-reset PCIe restore for a GA100 function."""
from __future__ import annotations

import mmap
import os
import struct
import time

DEFAULT_BDF = "0000:1a:00.0"
DEFAULT_SIZE = 16 << 20  # 16 MiB, the GA100 BAR0 window

NVIDIA_VENDOR_ID = 0x10DE
ALLOWED_GA100_DEVICES = {
    0x20C2,  # CMP 170HX 8GB
    0x2082,  # CMP 170HX 10GB
    0x20B5,  # A100 PCIe 80GB
    0x20B0,  # A100 SXM4 40GB
    0x20B2,  # A100 SXM4 80GB
    0x20F1,  # A100 PCIe 40GB
}
FORBIDDEN_DRIVERS = {"nvidia", "nouveau"}


def verify_ga100_pci(bdf: str, refuse_gpu_driver: bool = True) -> tuple[int, int]:
    """Verify sysfs BDF is an NVIDIA GA100 GPU and not bound to nvidia/nouveau."""
    dev_dir = f"/sys/bus/pci/devices/{bdf}"
    try:
        with open(f"{dev_dir}/vendor", encoding="ascii") as f:
            vendor = int(f.read().strip(), 16)
        with open(f"{dev_dir}/device", encoding="ascii") as f:
            device = int(f.read().strip(), 16)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"PCI device {bdf} not found ({dev_dir})") from e
    if vendor != NVIDIA_VENDOR_ID or device not in ALLOWED_GA100_DEVICES:
        raise RuntimeError(
            f"refused PCI device {bdf}: {vendor:04x}:{device:04x} is not a supported GA100 GPU"
        )
    if refuse_gpu_driver:
        drv_link = f"{dev_dir}/driver"
        if os.path.islink(drv_link):
            drv = os.path.basename(os.readlink(drv_link))
            if drv in FORBIDDEN_DRIVERS:
                raise RuntimeError(
                    f"refused PCI device {bdf}: driver '{drv}' is bound (unload before BAR0/config access)"
                )
    return vendor, device


def _resource0_start(bdf: str) -> int:
    path = f"/sys/bus/pci/devices/{bdf}/resource"
    try:
        with open(path) as f:
            return int(f.readline().split()[0], 16)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"PCI device {bdf} not found ({path})") from e


def restore(bdf: str = DEFAULT_BDF, wait_s: float = 0.25,
            settle_s: float = 0.15) -> bool:
    """Wait for PCIe link after PEX_RST, verify GA100 ID, reprogram BAR0, and enable mem decode.

    Returns False if the GPU never came back on PCIe config space within wait_s
    (normal on a failed glitch shot; caller should issue the next reset).
    Raises RuntimeError if BDF is not a supported GA100 GPU or is bound to nvidia/nouveau.
    """
    expected_vendor, expected_device = verify_ga100_pci(bdf, refuse_gpu_driver=True)
    if settle_s > 0:
        time.sleep(min(settle_s, wait_s))
    start = _resource0_start(bdf)
    cfg_path = f"/sys/bus/pci/devices/{bdf}/config"
    deadline = time.monotonic() + max(0.0, wait_s - settle_s)
    try:
        fd = os.open(cfg_path, os.O_RDWR)
    except PermissionError as e:
        raise PermissionError(f"cannot open {cfg_path} (run with sudo)") from e
    except FileNotFoundError as e:
        raise FileNotFoundError(f"PCI device {bdf} not found ({cfg_path})") from e
    try:
        while True:
            vd = struct.unpack("<I", os.pread(fd, 4, 0x00))[0]
            if vd != 0xFFFFFFFF:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        live_vendor = vd & 0xFFFF
        live_device = (vd >> 16) & 0xFFFF
        if live_vendor != expected_vendor or live_device not in ALLOWED_GA100_DEVICES:
            raise RuntimeError(
                f"refused config restore on {bdf}: live ID {live_vendor:04x}:{live_device:04x} "
                f"does not match GA100 ({expected_vendor:04x}:{expected_device:04x})"
            )
        bar0 = struct.unpack("<I", os.pread(fd, 4, 0x10))[0]
        if (bar0 & 0xFFFFFFF0) != (start & 0xFFFFFFF0):
            os.pwrite(fd, struct.pack("<I", start & 0xFFFFFFFF), 0x10)
        cmd = struct.unpack("<H", os.pread(fd, 2, 0x04))[0]
        if (cmd & 0x0002) == 0:
            os.pwrite(fd, struct.pack("<H", cmd | 0x0002), 0x04)
        vd_after = struct.unpack("<I", os.pread(fd, 4, 0x00))[0]
        return (vd_after & 0xFFFF) == expected_vendor and ((vd_after >> 16) & 0xFFFF) in ALLOWED_GA100_DEVICES
    finally:
        os.close(fd)


class Bar0:
    """mmap of /sys/bus/pci/devices/<bdf>/resource0 with alignment, bounds, and PCI ID guards."""

    def __init__(self, bdf: str = DEFAULT_BDF, size: int = DEFAULT_SIZE,
                 read_only: bool = False, mm=None, verify_pci: bool = True):
        self.bdf = bdf
        self.size = size
        self.read_only = bool(read_only)
        self._owns_mm = mm is None
        self.fd: int | None = None
        if mm is not None:
            self.mm = mm
            self.size = len(mm)
            return
        if verify_pci:
            verify_ga100_pci(bdf, refuse_gpu_driver=True)
        self.path = f"/sys/bus/pci/devices/{bdf}/resource0"
        flags = os.O_RDONLY if self.read_only else (os.O_RDWR | os.O_SYNC)
        prot = mmap.PROT_READ if self.read_only else (mmap.PROT_READ | mmap.PROT_WRITE)
        self.fd = os.open(self.path, flags)
        try:
            self.mm = mmap.mmap(self.fd, size, mmap.MAP_SHARED, prot)
        except Exception:
            os.close(self.fd)
            self.fd = None
            raise

    def _check_off(self, off: int) -> None:
        if not isinstance(off, int) or isinstance(off, bool) or (off & 3) or off < 0 or off + 4 > self.size:
            raise ValueError(f"invalid/unaligned BAR0 offset 0x{off:X} (size=0x{self.size:X})")

    def rd32(self, off: int) -> int:
        self._check_off(off)
        mv = memoryview(self.mm)[off:off + 4].cast("I")
        try:
            return int(mv[0])
        finally:
            mv.release()

    def wr32(self, off: int, value: int) -> None:
        if self.read_only:
            raise PermissionError(f"refused BAR0 write to 0x{off:06X} on read-only mapping")
        self._check_off(off)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 0xFFFFFFFF:
            raise ValueError(f"32-bit BAR0 write value out of range: {value!r}")
        # One aligned 32-bit store. struct.pack_into zeroes its destination
        # before packing, which on this mmap is a write of 0 to the register.
        mv = memoryview(self.mm)[off:off + 4].cast("I")
        try:
            mv[0] = value
        finally:
            mv.release()

    def close(self) -> None:
        if self._owns_mm and self.mm is not None:
            self.mm.close()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> "Bar0":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# The PROM (VBIOS + InfoROM flash) is shadowed at BAR0 + 0x300000; the InfoROM lives
# at PROM offset 0xC0000-0x100000. Its BRD object carries the board serial as ASCII
# digits 0x28 bytes into the object, behind a 0x10-byte wrapper.
PROM = 0x300000
PROM_SIZE = 0x100000
INFOROM_START, INFOROM_END = 0xC0000, 0x100000
BRD_MAGIC = b"\x02BRD"
BRD_SERIAL = 0x10 + 0x28


def brd_serial(bar: Bar0) -> str | None:
    """Board serial from the InfoROM BRD object, or None if there is none."""
    rom = bytes(bar.mm[PROM:PROM + PROM_SIZE])
    if rom[:4] != b"NVGI":
        return None
    i = rom.find(BRD_MAGIC, INFOROM_START, INFOROM_END)
    while i >= 0:
        serial = rom[i + BRD_SERIAL:i + BRD_SERIAL + 16].rstrip(b"\0")
        if serial.isdigit() and 8 <= len(serial) <= 15:
            return serial.decode()
        i = rom.find(BRD_MAGIC, i + 1, INFOROM_END)
    return None
