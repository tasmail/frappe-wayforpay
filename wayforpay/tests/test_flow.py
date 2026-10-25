import json
from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from wayforpay import api, checkout, signature

SECRET = "flow-secret"
GATEWAY = "Test"


def settings():
	if frappe.db.exists("WayForPay Settings", GATEWAY):
		frappe.delete_doc("WayForPay Settings", GATEWAY, force=True)
	doc = frappe.get_doc(
		{
			"doctype": "WayForPay Settings",
			"gateway_name": GATEWAY,
			"merchant_account": "test_merch",
			"merchant_domain_name": "shop.example",
			"merchant_secret_key": SECRET,
		}
	)
	doc.flags.ignore_mandatory = True
	return doc.insert(ignore_permissions=True)


def notification(reference, **override):
	data = {
		"merchantAccount": "test_merch",
		"orderReference": reference,
		"amount": 4520,
		"currency": "UAH",
		"authCode": "1",
		"cardPan": "41****1111",
		"transactionStatus": "Approved",
		"reason": "Ok",
		"reasonCode": 1100,
		**override,
	}
	text = {key: str(value) for key, value in data.items()}
	data["merchantSignature"] = signature.notification_signature(SECRET, text)
	return json.dumps(data)


class TestWayForPay(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.gateway = settings()
		self.todo = frappe.get_doc({"doctype": "ToDo", "description": "paid thing"}).insert().name
		url = self.gateway.get_payment_url(
			amount=4520,
			currency="UAH",
			title="Shop",
			description="Order 1",
			payer_email="payer@example.com",
			payer_name="Payer",
			reference_doctype="ToDo",
			reference_docname=self.todo,
			payment_gateway="WayForPay-Test",
		)
		self.order = url.split("order=")[1]

	def say(self, _order, _reference=""):
		frappe.msgprint("Payment Entry created")  # what the booking of a payment says to the visitor
		return "/orders/1"

	def send(self, body):
		frappe.local.response = frappe._dict()
		request = SimpleNamespace(get_data=lambda as_text=True: body)
		with (
			mock.patch.object(frappe, "request", request),
			mock.patch.object(api, "_pay", return_value="/orders/1", side_effect=self.say) as pay,
		):
			api.notify()
		return frappe.local.response, pay

	def test_saving_the_settings_makes_the_payment_gateway(self):
		self.assertTrue(frappe.db.exists("Payment Gateway", "WayForPay-Test"))
		self.assertEqual(
			frappe.db.get_value("Payment Gateway", "WayForPay-Test", "gateway_controller"), GATEWAY
		)

	def test_only_the_currencies_of_wayforpay_are_taken(self):
		self.gateway.validate_transaction_currency("UAH")
		with self.assertRaises(frappe.ValidationError):
			self.gateway.validate_transaction_currency("JPY")

	def test_the_form_is_signed_and_the_amount_comes_from_the_server_not_from_the_address(self):
		form = checkout.payment_form(self.order)
		fields = dict(form["fields"])

		self.assertEqual(form["action"], "https://secure.wayforpay.com/pay")
		self.assertEqual((fields["amount"], fields["currency"]), ("4520", "UAH"))
		self.assertEqual(fields["orderReference"], self.order)
		self.assertEqual(fields["productPrice[]"], "4520")
		self.assertTrue(fields["serviceUrl"].endswith("/api/method/wayforpay.api.notify"))
		expected = signature.sign_request(
			SECRET,
			{**fields, "orderDate": fields["orderDate"]},
			[{"name": "Order 1", "count": 1, "price": "4520"}],
		)
		self.assertEqual(fields["merchantSignature"], expected)

	def test_an_unknown_order_is_refused(self):
		with self.assertRaises(frappe.DoesNotExistError):
			checkout.payment_form("nothing")

	def test_an_approved_notification_completes_the_payment_once(self):
		response, pay = self.send(notification(self.order))

		self.assertEqual(response["status"], "accept")
		self.assertEqual(frappe.local.message_log, [])  # nothing but the answer goes to the gateway
		self.assertEqual(
			response["signature"], signature.sign(SECRET, [self.order, "accept", response["time"]])
		)
		pay.assert_called_once()
		self.assertEqual(frappe.db.get_value("Integration Request", self.order, "status"), "Completed")
		self.assertEqual(api.status(self.order), {"status": "Completed", "redirect_to": "/orders/1"})

		_response, again = self.send(notification(self.order))
		again.assert_not_called()  # a repeated notification pays nothing twice

	def test_a_notification_with_a_wrong_signature_or_amount_changes_nothing(self):
		forged = json.loads(notification(self.order))
		forged["merchantSignature"] = "0" * 32
		response, pay = self.send(json.dumps(forged))
		self.assertEqual(response.get("http_status_code"), 400)
		pay.assert_not_called()

		response, pay = self.send(
			notification(self.order, amount=1)
		)  # genuine, but not the amount of the order
		self.assertEqual(response.get("http_status_code"), 400)
		pay.assert_not_called()
		self.assertEqual(frappe.db.get_value("Integration Request", self.order, "status"), "Queued")

	def test_a_declined_payment_is_failed_and_an_unknown_reference_is_refused(self):
		self.send(notification(self.order, transactionStatus="Declined", reasonCode=1101))
		self.assertEqual(api.status(self.order), {"status": "Failed", "redirect_to": None})

		response, _pay = self.send(notification("nope"))
		self.assertEqual(response.get("http_status_code"), 400)

	def test_a_payment_in_process_is_pending(self):
		self.send(notification(self.order, transactionStatus="InProcessing"))
		self.assertEqual(api.status(self.order), {"status": "Pending"})


class TestPages(FrappeTestCase):
	def test_the_pages_have_python_controllers_that_frappe_can_find(self):
		"""A page whose file name has a dash gets no context (the names of the pages are python modules)."""
		import importlib

		for name in ("wayforpay_checkout", "wayforpay_result"):
			module = importlib.import_module(f"wayforpay.www.{name}")
			self.assertTrue(callable(module.get_context))


class TestOnPaid(FrappeTestCase):
	def test_a_function_named_in_the_order_is_called_with_the_order_and_the_reference(self):
		calls = []

		def handler(order, reference):
			calls.append((order["team"], reference))
			return "/dashboard/billing"

		with mock.patch("frappe.get_attr", return_value=handler):
			result = api._pay({"on_paid": "any.dotted.path", "team": "t1"}, "ref1")

		self.assertEqual(result, "/dashboard/billing")
		self.assertEqual(calls, [("t1", "ref1")])
		frappe.set_user("Administrator")

	def test_an_order_with_nothing_to_tell_is_left_alone(self):
		self.assertIsNone(api._pay({"amount": 1}, "ref1"))


class TestFailedRedirect(FrappeTestCase):
	def test_the_order_can_name_the_page_a_payer_goes_to_after_a_refusal(self):
		frappe.set_user("Administrator")
		gateway = settings()
		todo = frappe.get_doc({"doctype": "ToDo", "description": "x"}).insert().name
		url = gateway.get_payment_url(
			amount=10,
			currency="UAH",
			description="Order",
			reference_doctype="ToDo",
			reference_docname=todo,
			failed_redirect="/dashboard/billing",
		)
		order = url.split("order=")[1]
		frappe.db.set_value("Integration Request", order, "status", "Failed")

		self.assertEqual(api.status(order), {"status": "Failed", "redirect_to": "/dashboard/billing"})


class TestPayerEmail(FrappeTestCase):
	def test_a_payer_without_a_valid_address_sends_no_email_to_wayforpay(self):
		frappe.set_user("Administrator")
		gateway = settings()
		todo = frappe.get_doc({"doctype": "ToDo", "description": "x"}).insert().name
		names = []
		for payer in ("Administrator", "payer@example.com"):
			url = gateway.get_payment_url(
				amount=10,
				currency="UAH",
				description="Order",
				payer_email=payer,
				reference_doctype="ToDo",
				reference_docname=todo,
			)
			names.append(dict(checkout.payment_form(url.split("order=")[1])["fields"]).get("clientEmail"))

		self.assertEqual(names, [None, "payer@example.com"])


class TestReconcile(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.gateway = settings()
		self.todo = frappe.get_doc({"doctype": "ToDo", "description": "x"}).insert().name
		url = self.gateway.get_payment_url(
			amount=4520,
			currency="UAH",
			description="Order",
			reference_doctype="ToDo",
			reference_docname=self.todo,
		)
		self.order = url.split("order=")[1]

	def answer(self, **override):
		return json.loads(notification(self.order, **override))

	def run_reconcile(self, data):
		from wayforpay import reconcile

		with (
			mock.patch.object(reconcile, "check_status", return_value=data),
			mock.patch.object(api, "_pay", return_value="/orders/1") as pay,
		):
			return reconcile.reconcile_payment(self.order), pay

	def test_a_payment_that_lost_its_notification_is_paid_from_the_answer_of_the_check(self):
		status, pay = self.run_reconcile(signature.parse_notification(notification(self.order)))

		self.assertEqual(status, "Approved")
		pay.assert_called_once()
		self.assertEqual(frappe.db.get_value("Integration Request", self.order, "status"), "Completed")

	def test_an_answer_with_a_wrong_signature_or_amount_pays_nothing(self):
		forged = signature.parse_notification(notification(self.order))
		forged["merchantSignature"] = "0" * 32
		status, pay = self.run_reconcile(forged)
		self.assertIsNone(status)
		pay.assert_not_called()

		status, pay = self.run_reconcile(signature.parse_notification(notification(self.order, amount=1)))
		self.assertIsNone(status)
		pay.assert_not_called()
		self.assertEqual(frappe.db.get_value("Integration Request", self.order, "status"), "Queued")

	def test_a_payment_that_wayforpay_does_not_know_is_left_alone(self):
		status, pay = self.run_reconcile(None)

		self.assertIsNone(status)
		pay.assert_not_called()

	def test_only_payments_that_waited_long_enough_are_asked_about(self):
		from wayforpay import reconcile

		with mock.patch.object(reconcile, "reconcile_payment", return_value="Approved") as asked:
			self.assertNotIn(self.order, reconcile.reconcile())  # the payment of this test is a minute old
		self.assertNotIn(mock.call(self.order), asked.call_args_list)

		frappe.db.set_value(
			"Integration Request", self.order, "creation", frappe.utils.add_to_date(None, minutes=-30)
		)
		with mock.patch.object(reconcile, "reconcile_payment", return_value="Approved") as asked:
			self.assertEqual(reconcile.reconcile()[self.order], "Approved")
		self.assertIn(mock.call(self.order), asked.call_args_list)


class TestRefund(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.gateway = settings()
		todo = frappe.get_doc({"doctype": "ToDo", "description": "x"}).insert().name
		url = self.gateway.get_payment_url(
			amount=4520,
			currency="UAH",
			description="Order",
			reference_doctype="ToDo",
			reference_docname=todo,
			on_refunded="any.dotted.refund",
		)
		self.order = url.split("order=")[1]

	def run_reconcile(self, **extra):
		from wayforpay import reconcile

		data = signature.parse_notification(notification(self.order))
		with (
			mock.patch.object(reconcile, "check_status", return_value={**data, **extra}),
			mock.patch.object(api, "_pay", return_value="/orders/1") as pay,
		):
			return reconcile.reconcile_payment(self.order), pay

	def test_a_payment_that_was_returned_is_not_booked_by_the_check_of_its_state(self):
		status, pay = self.run_reconcile(refundAmount="4520")

		self.assertEqual(status, "Refunded")
		pay.assert_not_called()
		self.assertEqual(frappe.db.get_value("Integration Request", self.order, "status"), "Queued")

	def test_a_booked_payment_that_is_returned_later_is_told_to_the_shop_once(self):
		self.run_reconcile()  # booked
		calls = []

		def handler(*args):
			calls.append(args)

		with mock.patch("frappe.get_attr", return_value=handler):
			self.run_reconcile(refundAmount="4520")
			self.run_reconcile(refundAmount="4520")

		# the hooks of Frappe also call get_attr: only the call of the refund has the order as its second argument
		refunds = [call for call in calls if len(call) == 3 and call[1] == self.order]
		self.assertEqual(len(refunds), 1)  # the second answer changes nothing
		frappe.set_user("Administrator")
