import unittest

from ga100vfi.glitch import Attempt, Card


class FragmentedSerial:
    def __init__(self):
        self.parts = [b"timing i=0 scheduled=-1300 act=8 arg=32 start=-1300 end=-1264 ok=1\n",
                      b"glitch done", b" pwd=0 e1=0xc2ea writes_failed=12", b"\n"]
        self.sent = []

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.sent.append(data)

    def flush(self):
        pass

    def read(self, _size):
        return self.parts.pop(0)


class SerialResponseTest(unittest.TestCase):
    def test_completion_marker_waits_for_full_line(self):
        card = Card.__new__(Card)
        card.s = FragmentedSerial()
        report = card.attempt(Attempt())
        self.assertEqual(report.writes_failed, 12)
        self.assertEqual(report.e1, 0xC2EA)
        self.assertIn("timing i=0", report.raw)
        self.assertEqual(card.s.parts, [])
        self.assertEqual(len(card.s.sent), 1)


if __name__ == "__main__":
    unittest.main()
