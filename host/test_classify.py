"""Offline tests for classify(), Bar0 guards, and Card protocol parsing. Run from host/:
python3 -m unittest test_classify"""
import os
import unittest

from ga100vfi.bar0 import Bar0
from ga100vfi.classify import (
    CRC_STATUS,
    DSOV,
    DSOV_SET,
    FPF_ROW,
    FUSECTRL,
    PRIV_MASK,
    SHADOW,
    UNREADABLE,
    classify,
)
from ga100vfi.glitch import Attempt, Card

HEALTHY = {
    FUSECTRL: 0xE0040000,
    DSOV: 1,
    FPF_ROW: 0x00000000,
    DSOV_SET: 0,
    PRIV_MASK: 0xFFFFFFFF,
    CRC_STATUS: 0x55,
}


class FakeBar:
    def __init__(self, **over):
        self.regs = dict(HEALTHY)
        for k, v in over.items():
            if k in globals():
                self.regs[globals()[k]] = v
            elif k in SHADOW:
                self.regs[SHADOW[k]] = v
            else:
                raise KeyError(k)
        self.log = []

    def rd32(self, off):
        self.log.append(("rd", off))
        return self.regs.get(off, 0)

    def wr32(self, off, value):
        self.log.append(("wr", off, value))
        self.regs[off] = value

    def writes(self):
        return [x for x in self.log if x[0] == "wr"]

    def reads(self):
        return [x[1] for x in self.log if x[0] == "rd"]


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("CLASSIFY_WRITE", None)

    def tearDown(self):
        os.environ.pop("CLASSIFY_WRITE", None)
        if self._env is not None:
            os.environ["CLASSIFY_WRITE"] = self._env

    def test_default_writes_nothing(self):
        bar = FakeBar()
        v = classify(bar)
        self.assertEqual(bar.writes(), [])
        self.assertEqual(bar.reads()[:7], [FUSECTRL, DSOV, FPF_ROW, DSOV_SET, PRIV_MASK, CRC_STATUS,
                                           list(SHADOW.values())[0]])
        self.assertFalse(v.dsov_written)
        self.assertNotIn("dsov_settable", v.checks)
        self.assertEqual(v.dsov, 1)
        self.assertFalse(v.passed)

    def test_goldilocks_without_the_write(self):
        bar = FakeBar(DSOV=0)
        v = classify(bar)
        self.assertEqual(bar.writes(), [])
        self.assertEqual(v.dsov_writeback, 0)          # EN_SW_OVERRIDE read, not set
        self.assertTrue(v.passed, v.checks)

    def test_write_opt_in_by_environment(self):
        os.environ["CLASSIFY_WRITE"] = "1"
        bar = FakeBar(DSOV=0)
        v = classify(bar)
        self.assertEqual(bar.writes(), [("wr", DSOV_SET, 1)])
        self.assertTrue(v.dsov_written)
        self.assertTrue(v.checks["dsov_settable"])
        self.assertTrue(v.passed)
        os.environ["CLASSIFY_WRITE"] = "0"
        bar = FakeBar()
        classify(bar)
        self.assertEqual(bar.writes(), [])

    def test_write_argument_overrides_environment(self):
        os.environ["CLASSIFY_WRITE"] = "1"
        bar = FakeBar()
        classify(bar, write=False)
        self.assertEqual(bar.writes(), [])
        os.environ.pop("CLASSIFY_WRITE")
        bar = FakeBar()
        v = classify(bar, write=True)
        self.assertEqual(bar.writes(), [("wr", DSOV_SET, 1)])
        self.assertEqual(v.dsov_writeback, 1)

    def test_write_that_does_not_stick_fails_check_5(self):
        class Stuck(FakeBar):
            def wr32(self, off, value):
                self.log.append(("wr", off, value))
        bar = Stuck(DSOV=0)
        v = classify(bar, write=True)
        self.assertFalse(v.checks["dsov_settable"])
        self.assertFalse(v.passed)

    def test_poisoned_fusectrl_reads_nothing_else(self):
        os.environ["CLASSIFY_WRITE"] = "1"
        bar = FakeBar(FUSECTRL=0xBADF5040)
        v = classify(bar)
        self.assertEqual(bar.log, [("rd", FUSECTRL)])
        self.assertEqual((v.dsov, v.priv_mask, v.crc_status, v.fpf_row), (0, 0, 0, UNREADABLE))
        self.assertEqual(v.shadow, {"FUSECTRL_POISONED": 0xBADF5040})
        self.assertEqual(v.fault, "FUSECTRL_POISONED")
        self.assertFalse(v.passed)

    def test_dead_or_non_idle_fusectrl_stops_after_one_read_and_never_passes(self):
        os.environ["CLASSIFY_WRITE"] = "1"
        for bad_ctrl, expected_fault in (
            (0xFFFFFFFF, "FUSECTRL_DEAD"),
            (0xE0010001, "FUSECTRL_NOT_IDLE"),
            (0xE0040001, "FUSECTRL_NOT_IDLE"),
            (0x00000000, "FUSECTRL_NOT_IDLE"),
        ):
            with self.subTest(ctrl=hex(bad_ctrl)):
                bar = FakeBar(FUSECTRL=bad_ctrl, DSOV=0)
                v = classify(bar)
                self.assertEqual(bar.log, [("rd", FUSECTRL)])
                self.assertEqual(v.fault, expected_fault)
                self.assertFalse(v.passed)

    def test_poisoned_or_dead_dsov_stops_before_any_write(self):
        os.environ["CLASSIFY_WRITE"] = "1"
        for bad_dsov, key in ((0xBADF1701, "DSOV_POISONED"), (0xBAD00100, "DSOV_POISONED"),
                              (0xFFFFFFFF, "DSOV_DEAD")):
            with self.subTest(dsov=hex(bad_dsov)):
                bar = FakeBar(DSOV=bad_dsov)
                v = classify(bar)
                self.assertEqual(bar.log, [("rd", FUSECTRL), ("rd", DSOV)])
                self.assertEqual(v.dsov, bad_dsov)
                self.assertEqual((v.priv_mask, v.crc_status, v.fpf_row), (0, 0, UNREADABLE))
                self.assertEqual(v.shadow, {key: bad_dsov})
                self.assertEqual(v.fault, key)
                self.assertFalse(v.passed)

    def test_poisoned_bad0_fusectrl_reads_nothing_else(self):
        os.environ["CLASSIFY_WRITE"] = "1"
        bar = FakeBar(FUSECTRL=0xBAD00001)
        v = classify(bar)
        self.assertEqual(bar.log, [("rd", FUSECTRL)])
        self.assertEqual(v.shadow, {"FUSECTRL_POISONED": 0xBAD00001})
        self.assertFalse(v.passed)

    def test_poisoned_shadow_or_crc_aborts_immediately_and_fails_passed(self):
        # Even when DSOV/FPF/PRIV/CRC look Goldilocks, a poisoned shadow register
        # MUST stop reading remaining shadows and set passed=False!
        bar = FakeBar(DSOV=0, STATUS_OPT_GPC=0xBAD01234)
        v = classify(bar)
        self.assertFalse(v.passed)
        self.assertEqual(v.fault, "SHADOW_POISONED:STATUS_OPT_GPC")
        # Stopped right after reading the first shadow register
        self.assertEqual(len(v.shadow), 1)

        bar_crc = FakeBar(DSOV=0, CRC_STATUS=0xBADF0055)
        v_crc = classify(bar_crc)
        self.assertFalse(v_crc.passed)
        self.assertEqual(v_crc.fault, "CRC_POISONED")
        self.assertEqual(bar_crc.reads(), [FUSECTRL, DSOV, FPF_ROW, DSOV_SET, PRIV_MASK, CRC_STATUS])

    def test_other_bad_dsov_values_are_not_goldilocks(self):
        for dsov in (2, 0xFFFFFFFF):
            with self.subTest(dsov=hex(dsov)):
                v = classify(FakeBar(DSOV=dsov))
                self.assertFalse(v.passed)

    def test_as_dict_reports_the_write_mode(self):
        self.assertFalse(classify(FakeBar()).as_dict()["dsov_written"])
        self.assertTrue(classify(FakeBar(), write=True).as_dict()["dsov_written"])

    def test_bar0_alignment_bounds_and_read_only(self):
        buf = bytearray(64)
        bar = Bar0(mm=buf, read_only=False)
        bar.wr32(0x10, 0xCAFEBABE)
        self.assertEqual(bar.rd32(0x10), 0xCAFEBABE)
        for bad in (-4, 1, 2, 64, 62):
            with self.assertRaises(ValueError):
                bar.rd32(bad)
            with self.assertRaises(ValueError):
                bar.wr32(bad, 0)
        ro = Bar0(mm=buf, read_only=True)
        with self.assertRaises(PermissionError):
            ro.wr32(0x10, 0)

    def test_card_unlock_attempt_revert_reject_err_receipts(self):
        class StubCard(Card):
            def __init__(self, reply):
                self._reply = reply

            def cmd(self, line, wait=0.3, total=3.0, until=None):
                return self._reply

        self.assertTrue(StubCard("unlock already MATCH (CML 0x08)\nunlock MATCH wp=00 cfg=1229").unlock())
        self.assertFalse(StubCard("unlock MATCH wp=00 cfg=1229 write=ERR").unlock())
        self.assertFalse(StubCard("unlock ERR wp=00 cfg=1229").unlock())
        self.assertFalse(StubCard("unlock LOCKED wp=00 cfg=1229").unlock())

        with self.assertRaises(IOError):
            StubCard("ERR: line too long (rejected)").attempt(Attempt())
        with self.assertRaises(IOError):
            StubCard("glitch ABORT: expected 12 integer args, got 1").attempt(Attempt())
        rep_fail = StubCard("glitch FAIL: post-shot read\nglitch done pwd=1 e1=0xc2fa writes_failed=0").attempt(Attempt())
        self.assertGreaterEqual(rep_fail.writes_failed, 1)

        with self.assertRaises(IOError):
            StubCard("revert FAIL vmin=0 vout=1 mode=1").revert()
        self.assertIn("PWM-VID restored", StubCard("revert vmin=1 vout=1 mode=1 (PWM-VID restored)").revert())


if __name__ == "__main__":
    unittest.main()
