#

import unittest

import earningsbot


class TestEarningsPublisher(unittest.TestCase):
    def test_positive_earnings(self):
        example_message = {
            "body": "$AAPL reported earnings of $1.20",
            "symbols": [{"title": "Apple Inc"}]
        }
        earnings_publisher = earningsbot.EarningsPublisher(example_message)
        self.assertEqual(earnings_publisher.earnings, 1.2)
        self.assertEqual(earnings_publisher._earnings, "1.20")

    def test_negative_earnings(self):
        example_message = {
            "body": "$TSLA reported a loss of $0.50",
            "symbols": [{"title": "Tesla Inc"}]
        }
        earnings_publisher = earningsbot.EarningsPublisher(example_message)
        self.assertEqual(earnings_publisher.earnings, -0.5)
        self.assertEqual(earnings_publisher._earnings, "-0.50")

    def test_positive_consensus(self):
        example_message = {
            "body": "$FB reported earnings of $1.20, consensus was $1.00",
            "symbols": [{"title": "Facebook Inc"}]
        }
        earnings_publisher = earningsbot.EarningsPublisher(example_message)
        self.assertEqual(earnings_publisher.consensus, 1.0)
        self.assertEqual(earnings_publisher._consensus, "1.00")

    def test_negative_consensus(self):
        example_message = {
            "body": "$GOOG reported earnings of $1.20, consensus was ($1.00)",
            "symbols": [{"title": "Google Inc"}]
        }
        earnings_publisher = earningsbot.EarningsPublisher(example_message)
        self.assertEqual(earnings_publisher.consensus, -1.0)
        self.assertEqual(earnings_publisher._consensus, "-1.00")

    def test_no_consensus(self):
        example_message = {
            "body": "$NFLX report does not mention consensus",
            "symbols": [{"title": "Netflix Inc"}]
        }
        earnings_publisher = earningsbot.EarningsPublisher(example_message)
        self.assertIsNone(earnings_publisher.consensus)

    def test_earnings_with_consensus(self):
        example_message = {
            "body": "$AMZN reported earnings of $5.00, consensus was $4.50",
            "symbols": [{"title": "Amazon Inc"}]
        }
        earnings_publisher = earningsbot.EarningsPublisher(example_message)
        self.assertEqual(earnings_publisher.consensus, 4.50)
        self.assertEqual(earnings_publisher._consensus, "4.50")
        self.assertEqual(earnings_publisher.earnings, 5.00)
        self.assertEqual(earnings_publisher._earnings, "5.00")
        self.assertTrue(earnings_publisher.winner)

    def test_earnings_with_negative_consensus(self):
        example_message = {
            "body": "$AMZN reported earnings of $5.00, consensus was ($4.50)",
            "symbols": [{"title": "Amazon Inc"}]
        }
        earnings_publisher = earningsbot.EarningsPublisher(example_message)
        self.assertEqual(earnings_publisher.consensus, -4.50)
        self.assertEqual(earnings_publisher._consensus, "-4.50")
        self.assertEqual(earnings_publisher.earnings, 5.00)
        self.assertEqual(earnings_publisher._earnings, "5.00")
        self.assertTrue(earnings_publisher.winner)


    def test_earnings_with_negative_earnings(self):
        example_message = {
            "body": "$AMZN reported earnings of ($5.00), consensus was $4.50",
            "symbols": [{"title": "Amazon Inc"}]
        }
        earnings_publisher = earningsbot.EarningsPublisher(example_message)
        self.assertEqual(earnings_publisher.consensus, 4.50)
        self.assertEqual(earnings_publisher._consensus, "4.50")
        self.assertEqual(earnings_publisher.earnings, -5.00)
        self.assertEqual(earnings_publisher._earnings, "-5.00")
        self.assertFalse(earnings_publisher.winner)

    def test_earnings_without_consensus(self):
        example_message = {
            "body": "$AMZN reported earnings of $5.00",
            "symbols": [{"title": "Amazon Inc"}]
        }
        earnings_publisher = earningsbot.EarningsPublisher(example_message)
        self.assertIsNone(earnings_publisher.consensus)
        self.assertEqual(earnings_publisher.earnings, 5.00)
        self.assertEqual(earnings_publisher._earnings, "5.00")
        self.assertIsNone(earnings_publisher.winner)


if __name__ == "__main__":
    unittest.main()
