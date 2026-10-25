import hashlib
import hmac
import json
from unittest import TestCase

from wayforpay import signature

SECRET = "test-secret"


def md5(text: str) -> str:
	return hmac.new(SECRET.encode(), text.encode(), hashlib.md5).hexdigest()


class TestSignature(TestCase):
	def test_the_values_are_joined_with_a_semicolon_and_signed_with_hmac_md5(self):
		self.assertEqual(signature.sign(SECRET, ["a", 1, None, "b"]), md5("a;1;;b"))

	def test_a_request_is_signed_in_the_order_of_the_documentation_with_the_products_by_kind(self):
		fields = {
			"merchantAccount": "shop",
			"merchantDomainName": "shop.example",
			"orderReference": "R1",
			"orderDate": 1700000000,
			"amount": "300",
			"currency": "UAH",
		}
		products = [
			{"name": "A", "count": 1, "price": "100"},
			{"name": "B", "count": 2, "price": "100"},
		]

		self.assertEqual(
			signature.sign_request(SECRET, fields, products),
			md5("shop;shop.example;R1;1700000000;300;UAH;A;B;1;2;100;100"),
		)

	def test_an_amount_is_a_whole_number_or_has_two_digits(self):
		self.assertEqual(signature.format_amount(4520), "4520")
		self.assertEqual(signature.format_amount(4520.0), "4520")
		self.assertEqual(signature.format_amount("4520.5"), "4520.50")
		self.assertEqual(signature.format_amount(0.1 + 0.2), "0.30")

	def test_a_notification_is_genuine_only_with_its_own_signature(self):
		body = json.dumps(
			{
				"merchantAccount": "shop",
				"orderReference": "R1",
				"amount": 300.50,
				"currency": "UAH",
				"authCode": "123456",
				"cardPan": "41****1111",
				"transactionStatus": "Approved",
				"reasonCode": 1100,
			}
		)
		data = signature.parse_notification(body)
		data["merchantSignature"] = signature.notification_signature(SECRET, data)

		self.assertEqual(data["amount"], "300.5")  # the text that was sent, not a float again
		self.assertTrue(signature.notification_is_genuine(SECRET, data))
		self.assertFalse(signature.notification_is_genuine("other", data))
		self.assertFalse(signature.notification_is_genuine(SECRET, {**data, "amount": "1"}))
		self.assertFalse(signature.notification_is_genuine(SECRET, {**data, "merchantSignature": ""}))

	def test_the_answer_to_a_notification_is_signed_with_its_reference_status_and_time(self):
		reply = signature.answer(SECRET, "R1", 1700000000)

		self.assertEqual(reply["signature"], md5("R1;accept;1700000000"))
		self.assertEqual(
			(reply["orderReference"], reply["status"], reply["time"]), ("R1", "accept", 1700000000)
		)
