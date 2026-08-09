import unittest

from tibo_codex_reset_monitor import classify, parse_feed


class MonitorTests(unittest.TestCase):
    def test_completed_reset(self):
        text = "I have reset usage limits for all paid users of ChatGPT Work and Codex."
        self.assertEqual(classify(text), "RESET COMPLETED")

    def test_incoming_reset(self):
        self.assertEqual(classify("I will reset Codex usage limits tonight."), "RESET INCOMING")

    def test_unrelated_reset(self):
        self.assertIsNone(classify("I feel Theo is in need of a reset."))

    def test_rejects_error_feed(self):
        payload = b"<rss><channel><title>RSS reader not yet whitelisted!</title><item><title>error</title></item></channel></rss>"
        with self.assertRaisesRegex(ValueError, "not owned"):
            parse_feed(payload)


if __name__ == "__main__":
    unittest.main()
