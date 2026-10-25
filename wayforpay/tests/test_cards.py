import json
from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from wayforpay import api, cards, charge, checkout, signature
from wayforpay.tests.test_flow import SECRET, settings


def verified(reference, **override):
	"""A signed result of a verification, as WayForPay sends it."""
	data = {
		"merchantAccount": "test_merch",
		"orderReference": reference,
		"amount": "0",
		"currency": "UAH",
		"authCode": "123456",
		"cardPan": "55****4723",
		"cardType": "MasterCard",
		"issuerBankName": "Test Bank",
		"recToken": "token-of-the-card-1",
		"transactionStatus": "Approved",
		"reason": "Ok",
		"reasonCode": "1100",
		**override,
	}
	data["merchantSignature"] = signature.notification_signature(SECRET, data)
	return json.dumps(data)


TOLD = []


def told_card(order, card):
	"""The function that a shop names as on_card: it is told the order and the name of the saved card."""
	TOLD.append(card)
	return "/dashboard/billing"


class TestCards(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.gateway = settings()
		self.todo = frappe.get_doc({"doctype": "ToDo", "description": "x"}).insert().name
		self.owner = ("ToDo", self.todo)

	def start(self, **extra):
		url = self.gateway.get_card_url(
			reference_doctype=self.owner[0],
			reference_docname=self.owner[1],
			payer_email="payer@example.com",
			**extra,
		)
		return url.split("order=")[1]

	def notify(self, body):
		frappe.local.response = frappe._dict()
		with mock.patch.object(frappe, "request", SimpleNamespace(get_data=lambda as_text=True: body)):
			api.notify()
		return frappe.local.response

	def test_the_form_verifies_the_card_for_nothing_and_is_signed_without_products(self):
		order = self.start()

		form = checkout.payment_form(order)
		fields = dict(form["fields"])

		self.assertEqual(form["action"], "https://secure.wayforpay.com/verify")
		self.assertEqual((fields["amount"], fields["currency"], fields["verifyType"]), ("0", "UAH", "simple"))
		self.assertNotIn("productName[]", fields)
		expected = signature.sign(SECRET, ["test_merch", "shop.example", order, "0", "UAH"])
		self.assertEqual(fields["merchantSignature"], expected)

	def test_a_verified_card_is_saved_with_its_token_and_the_token_is_not_in_the_log(self):
		order = self.start()

		response = self.notify(verified(order))

		self.assertEqual(response["status"], "accept")
		card = cards.active_card(*self.owner)
		self.assertIsNotNone(card)
		doc = frappe.get_doc("WayForPay Card", card)
		self.assertEqual((doc.card_pan, doc.card_type), ("55****4723", "MasterCard"))
		self.assertEqual(doc.token(), "token-of-the-card-1")
		output = frappe.db.get_value("Integration Request", order, "output")
		self.assertNotIn("token-of-the-card-1", output)
		self.assertEqual(frappe.db.get_value("Integration Request", order, "status"), "Completed")

	def test_a_new_card_replaces_the_old_one_and_the_old_token_is_forgotten(self):
		self.notify(verified(self.start()))
		old = cards.active_card(*self.owner)

		self.notify(verified(self.start(), recToken="token-of-the-card-2", cardPan="41****1111"))

		new = cards.active_card(*self.owner)
		self.assertNotEqual(old, new)
		self.assertEqual(frappe.db.get_value("WayForPay Card", old, "status"), "Removed")
		self.assertEqual(frappe.get_doc("WayForPay Card", old).token(), "")
		self.assertEqual(frappe.get_doc("WayForPay Card", new).token(), "token-of-the-card-2")

	def test_a_declined_verification_saves_nothing_and_a_verification_without_a_token_neither(self):
		self.notify(verified(self.start(), transactionStatus="Declined", reasonCode="1101", recToken=""))
		self.notify(verified(self.start(), recToken=""))

		self.assertIsNone(cards.active_card(*self.owner))

	def test_the_shop_is_told_about_the_new_card(self):
		TOLD.clear()
		order = self.start(on_card="wayforpay.tests.test_cards.told_card")

		self.notify(verified(order))

		card = cards.active_card(*self.owner)
		self.assertEqual(TOLD, [card])
		self.assertIn("/dashboard/billing", frappe.db.get_value("Integration Request", order, "output"))
		frappe.set_user("Administrator")


class TestCharge(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.gateway = settings()
		self.owner = ("ToDo", frappe.get_doc({"doctype": "ToDo", "description": "y"}).insert().name)
		self.card = frappe.get_doc(
			{
				"doctype": "WayForPay Card",
				"gateway": self.gateway.name,
				"owner_doctype": self.owner[0],
				"owner_name": self.owner[1],
				"status": "Active",
				"rec_token": "token-of-the-card-1",
			}
		).insert()

	def answer(self, status="Approved", **override):
		def post(url, json=None, timeout=None):
			data = {
				"merchantAccount": "test_merch",
				"orderReference": json["orderReference"],
				"amount": str(json["amount"]),
				"currency": json["currency"],
				"authCode": "1",
				"cardPan": "55****4723",
				"transactionStatus": status,
				"reason": "Ok",
				"reasonCode": "1100",
				**override,
			}
			data["merchantSignature"] = signature.notification_signature(SECRET, data)
			self.sent = json
			return SimpleNamespace(text=__import__("json").dumps(data), raise_for_status=lambda: None)

		return post

	def charge(self, **kwargs):
		with (
			mock.patch.object(charge.requests, "post", side_effect=self.answer(**kwargs)),
			mock.patch.object(api, "_pay", return_value="/dashboard/billing") as pay,
		):
			result = charge.charge_card(
				self.card.name, 226.01, "UAH", "Top up", payer_email="payer@example.com", payer_name="Payer"
			)
		return result, pay

	def test_a_charge_goes_by_the_token_without_card_data_and_books_the_payment(self):
		result, pay = self.charge()

		self.assertEqual(result["status"], "Approved")
		pay.assert_called_once()
		self.assertEqual(self.sent["transactionType"], "CHARGE")
		self.assertEqual(self.sent["recToken"], "token-of-the-card-1")
		self.assertNotIn("card", self.sent)
		expected = signature.sign_request(
			SECRET,
			{**self.sent, "amount": "226.01"},
			[{"name": "Top up", "count": 1, "price": "226.01"}],
		)
		self.assertEqual(self.sent["merchantSignature"], expected)
		self.assertEqual(
			frappe.db.get_value("Integration Request", result["reference"], "status"), "Completed"
		)

	def test_a_declined_charge_books_nothing(self):
		result, pay = self.charge(status="Declined", reasonCode="1101", reason="Not enough funds")

		self.assertEqual((result["status"], result["reason"]), ("Declined", "Not enough funds"))
		pay.assert_not_called()
		self.assertEqual(frappe.db.get_value("Integration Request", result["reference"], "status"), "Failed")

	def test_an_answer_that_is_not_signed_by_the_merchant_key_is_not_believed(self):
		def post(url, json=None, timeout=None):
			data = {
				"merchantAccount": "test_merch",
				"orderReference": json["orderReference"],
				"amount": "226.01",
			}
			data.update({"currency": "UAH", "transactionStatus": "Approved", "merchantSignature": "0" * 32})
			return SimpleNamespace(text=__import__("json").dumps(data), raise_for_status=lambda: None)

		with (
			mock.patch.object(charge.requests, "post", side_effect=post),
			mock.patch.object(api, "_pay") as pay,
		):
			result = charge.charge_card(self.card.name, 226.01, "UAH", "Top up")

		self.assertEqual(result["status"], "Error")
		pay.assert_not_called()

	def test_a_card_that_was_removed_is_not_charged(self):
		cards.remove_cards(*self.owner)

		with mock.patch.object(charge.requests, "post") as post:
			result = charge.charge_card(self.card.name, 10, "UAH", "Top up")

		self.assertEqual(result["status"], "NoCard")
		post.assert_not_called()
