import unittest

from resumable_page_fetcher import FakeClock


class ClockTests(unittest.TestCase):
    def test_fake_clock_advances_only_through_sleep(self):
        clock = FakeClock(start=50.0)
        self.assertEqual(clock.now(), 50.0)

        clock.sleep(2.5)
        clock.sleep(0.5)

        self.assertEqual(clock.now(), 53.0)
        self.assertEqual(clock.sleeps, [2.5, 0.5])

    def test_fake_clock_rejects_negative_sleep(self):
        with self.assertRaises(ValueError):
            FakeClock().sleep(-1.0)

if __name__ == "__main__":
    unittest.main()
