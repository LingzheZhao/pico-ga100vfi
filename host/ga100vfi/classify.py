"""The five per-attempt checks, in the order the method prescribes.

1. glitch (issued elsewhere)
2. sleep 200 ms
3. read FUSECTRL first — abort immediately on PRI poison, 0xFFFFFFFF, or non-IDLE
4. read DSOV (0x00820084); continue unless it reads 0
5. read an FPF row (0x00824314); continue if it reads 0xffffffff
6. write DSOV = 1 (0x00820040), read back; continue unless it reads 1
   (0x00820040 is EN_SW_OVERRIDE and keeps its value across PEX_RST; the write
   is opt-in, see classify())
7. dump the shadow fuse registers and compare with the baseline (logged only)
8. read NV_FUSE_OPT_PRIV_LEVEL_MASK (0x008200fc); Goldilocks reads 0xffffffff
9. read NV_FUSE_CRC_STATUS (0x00820070); continue unless it reads 0x55
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DSOV = 0x00820084
FPF_ROW = 0x00824314
DSOV_SET = 0x00820040
PRIV_MASK = 0x008200fc
CRC_STATUS = 0x00820070
CRC_GOOD = 0x55

FUSECTRL = 0x00820000
FUSECTRL_STATE_MASK = 0x1F << 16
FUSECTRL_STATE_IDLE = 4 << 16

# Shadow registers that move when the fuse block is misread.
SHADOW = {
    "STATUS_OPT_GPC": 0x00820C1C,
    "NV_FUSE_OPT_GPC_DEFECTIVE": 0x008205C4,
    "STATUS_OPT_FBP": 0x00820D38,
    "FBP_DEFECTIVE": 0x008205CC,
    "FBPA_DEFECTIVE": 0x008205D0,
}

UNREADABLE = 0xFFFFFFFF


@dataclass
class Verdict:
    dsov: int
    fpf_row: int
    dsov_writeback: int
    priv_mask: int
    crc_status: int
    shadow: dict[str, int] = field(default_factory=dict)
    shadow_diff: dict[str, tuple[int, int]] = field(default_factory=dict)
    # False when classify() did not write 0x00820040: dsov_writeback is then only
    # a read of that register and check 5 is left out of `checks`.
    dsov_written: bool = True
    # Non-None whenever FUSECTRL or any checked register returned poison, dead bus,
    # or non-IDLE controller state. Guarantees passed == False.
    fault: str | None = None

    @property
    def checks(self) -> dict[str, bool]:
        c = {
            "dsov_zero": self.dsov == 0,
            "fpf_readable": self.fpf_row != UNREADABLE,
            "dsov_settable": self.dsov_writeback == 1,
            "priv_mask_open": self.priv_mask == 0xFFFFFFFF,
            "crc_valid": self.crc_status == CRC_GOOD,
        }
        if not self.dsov_written:
            del c["dsov_settable"]
        return c

    @property
    def passed(self) -> bool:
        return self.fault is None and all(self.checks.values())

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "fault": self.fault,
            "checks": self.checks,
            "dsov": self.dsov,
            "fpf_row": self.fpf_row,
            "dsov_writeback": self.dsov_writeback,
            "dsov_written": self.dsov_written,
            "priv_mask": self.priv_mask,
            "crc_status": self.crc_status,
            "shadow": self.shadow,
            "shadow_diff": self.shadow_diff,
        }


def _poisoned(value: int) -> bool:
    """True for any NVIDIA PRI error sentinel 0xBAD0xxxx..0xBADFxxxx."""
    return ((value >> 20) & 0xFFF) == 0xBAD


def _ctrl_idle(ctrl: int) -> bool:
    """True only for healthy, idle FUSECTRL (not poisoned, not 0xFFFFFFFF, STATE==IDLE, CMD==0)."""
    return (
        not _poisoned(ctrl)
        and ctrl != UNREADABLE
        and (ctrl & FUSECTRL_STATE_MASK) == FUSECTRL_STATE_IDLE
        and (ctrl & 0xF) == 0
    )


def snapshot(bar, fail_fast: bool = False) -> dict[str, int]:
    """Read SHADOW registers. If fail_fast=True, stops on first poison or 0xFFFFFFFF."""
    out: dict[str, int] = {}
    for name, off in SHADOW.items():
        val = bar.rd32(off)
        out[name] = val
        if fail_fast and (_poisoned(val) or val == UNREADABLE):
            break
    return out


def classify(bar, baseline: dict[str, int] | None = None, write: bool | None = None) -> Verdict:
    """Run fail-fast fuse-controller health gate and checks 3-8.

    Read FUSECTRL first. A poisoned (0xBADxxxxx), dead (0xFFFFFFFF), or non-IDLE
    FUSECTRL aborts immediately after 1 read with passed=False and ZERO further
    BAR0 reads or writes. Subsequent registers also abort immediately on PRI
    poison or dead 0xFFFFFFFF (except PRIV_MASK, where 0xFFFFFFFF means open PLM,
    and FPF_ROW, where 0xFFFFFFFF simply fails fpf_readable without aborting
    unless poisoned).
    """
    if write is None:
        write = os.environ.get("CLASSIFY_WRITE") == "1"

    ctrl = bar.rd32(FUSECTRL)
    if _poisoned(ctrl):
        return Verdict(0, UNREADABLE, 0, 0, 0, {"FUSECTRL_POISONED": ctrl}, {}, write,
                       fault="FUSECTRL_POISONED")
    if ctrl == UNREADABLE:
        return Verdict(0, UNREADABLE, 0, 0, 0, {"FUSECTRL_DEAD": ctrl}, {}, write,
                       fault="FUSECTRL_DEAD")
    if not _ctrl_idle(ctrl):
        return Verdict(0, UNREADABLE, 0, 0, 0, {"FUSECTRL_NOT_IDLE": ctrl}, {}, write,
                       fault="FUSECTRL_NOT_IDLE")

    dsov = bar.rd32(DSOV)
    if _poisoned(dsov):
        return Verdict(dsov, UNREADABLE, 0, 0, 0, {"DSOV_POISONED": dsov}, {}, write,
                       fault="DSOV_POISONED")
    if dsov == UNREADABLE:
        return Verdict(dsov, UNREADABLE, 0, 0, 0, {"DSOV_DEAD": dsov}, {}, write,
                       fault="DSOV_DEAD")

    fpf = bar.rd32(FPF_ROW)
    if _poisoned(fpf):
        return Verdict(dsov, fpf, 0, 0, 0, {"FPF_POISONED": fpf}, {}, write,
                       fault="FPF_POISONED")

    if write:
        bar.wr32(DSOV_SET, 1)
    writeback = bar.rd32(DSOV_SET)
    if _poisoned(writeback) or writeback == UNREADABLE:
        tag = "DSOV_SET_POISONED" if _poisoned(writeback) else "DSOV_SET_DEAD"
        return Verdict(dsov, fpf, writeback, 0, 0, {tag: writeback}, {}, write, fault=tag)

    priv = bar.rd32(PRIV_MASK)
    if _poisoned(priv):
        return Verdict(dsov, fpf, writeback, priv, 0, {"PRIV_MASK_POISONED": priv}, {}, write,
                       fault="PRIV_MASK_POISONED")

    crc = bar.rd32(CRC_STATUS)
    if _poisoned(crc) or crc == UNREADABLE:
        tag = "CRC_POISONED" if _poisoned(crc) else "CRC_DEAD"
        return Verdict(dsov, fpf, writeback, priv, crc, {tag: crc}, {}, write, fault=tag)

    shadow: dict[str, int] = {}
    fault: str | None = None
    for name, off in SHADOW.items():
        val = bar.rd32(off)
        shadow[name] = val
        if _poisoned(val):
            fault = f"SHADOW_POISONED:{name}"
            break
        if val == UNREADABLE:
            fault = f"SHADOW_DEAD:{name}"
            break

    diff = {}
    if baseline and fault is None:
        diff = {k: (baseline.get(k), v) for k, v in shadow.items() if baseline.get(k) != v}
    return Verdict(dsov, fpf, writeback, priv, crc, shadow, diff, write, fault=fault)
