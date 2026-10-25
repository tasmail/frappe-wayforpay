import datetime
from unittest import TestCase

from wayforpay import rates

ROWS = [{"cc": "USD", "rate": 45.2021, "exchangedate": "26.10.2026"}]


class TestRates(TestCase):
	def test_the_rate_and_its_date_are_read_from_the_answer(self):
		self.assertEqual(rates.parse(ROWS, "USD"), (45.2021, datetime.date(2026, 10, 26)))

	def test_an_answer_without_the_currency_or_with_an_absurd_rate_is_refused(self):
		for rows in ([], [{"cc": "EUR", "rate": 49, "exchangedate": "26.10.2026"}], [{**ROWS[0], "rate": 0}]):
			with self.assertRaises(ValueError):
				rates.parse(rows, "USD")
