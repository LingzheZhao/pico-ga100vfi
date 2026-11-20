import unittest

from passive_snapshot import REGISTERS, observe


class PassiveSnapshotTest(unittest.TestCase):
    def run_reads(self, data):
        calls = []

        def read(address):
            calls.append(address)
            value = data[len(calls) - 1]
            if isinstance(value, Exception):
                raise value
            return value

        return observe(read), calls

    def test_baseline_accepts_open_privilege_mask(self):
        result, calls = self.run_reads([0xE0040000, 0x3FF, 1, 0x55, 0xFFFFFFFF])
        self.assertEqual(result["state"], "healthy_locked")
        self.assertEqual(calls, [offset for _, offset in REGISTERS])

    def test_stops_at_every_badf_position(self):
        good = [0xE0040000, 0x3FF, 1, 0x55, 0xFFFFFFFF]
        for index in range(len(good)):
            with self.subTest(index=index):
                result, calls = self.run_reads(good[:index] + [0xBADF5040])
                self.assertEqual(result["state"], "poisoned")
                self.assertEqual(len(calls), index + 1)
                for name, _ in REGISTERS[index + 1:]:
                    self.assertIsNone(result["registers"][name])

    def test_stops_on_booting_all_ones_and_io_error(self):
        for values, state in [([0xFFFFFFFF], "unknown"),
                              ([0xE0040000, 0x301], "booting"),
                              ([0xE0040000, OSError("read failed")], "unknown")]:
            with self.subTest(values=values):
                result, calls = self.run_reads(values)
                self.assertEqual(result["state"], state)
                self.assertEqual(len(calls), len(values))
                self.assertIsNone(result["registers"]["DSOV"])

    def test_stops_on_bad0_nonidle_fusectrl_and_unready_gfw(self):
        for values, state, reason in [([0xE0040000, 0x3FF, 0, 0xBAD00100], "poisoned", "CRC: BAD0"),
                                      ([0xE0040001], "unknown", "FUSECTRL: unexpected non-idle"),
                                      ([0xE0040000, 0x302], "booting", "not 0xff")]:
            with self.subTest(values=values):
                result, calls = self.run_reads(values)
                self.assertEqual(result["state"], state)
                self.assertIn(reason, result["reason"])
                self.assertEqual(len(calls), len(values))

    def test_candidate_is_not_active_validation(self):
        result, _ = self.run_reads([0xE0040000, 0x3FF, 0, 0x55, 0xFFFFFFFF])
        self.assertEqual(result["state"], "candidate")
        self.assertIn("active validation not performed", result["reason"])


if __name__ == "__main__":
    unittest.main()
