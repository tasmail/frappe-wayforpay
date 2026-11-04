from types import SimpleNamespace
from unittest import TestCase, mock

import frappe

from wayforpay import api, reconcile


class TestPickLive(TestCase):
	"""Pure Python: the choice of the request that takes a payment made for a cancelled one."""

	def test_the_newest_request_with_the_paid_amount_is_taken(self):
		rows = [{"name": "PRQ-3", "grand_total": 45.67}, {"name": "PRQ-2", "grand_total": 45.67}]
		self.assertEqual(api.pick_live(rows, 45.67), "PRQ-3")

	def test_the_amount_must_match_within_the_tolerance(self):
		rows = [{"name": "PRQ-3", "grand_total": 45.67}]
		self.assertEqual(api.pick_live(rows, 45.674), "PRQ-3")
		self.assertIsNone(api.pick_live(rows, 45.68))
		self.assertIsNone(api.pick_live(rows, 10))

	def test_no_request_no_choice(self):
		self.assertIsNone(api.pick_live([], 45.67))


class TestLiveRequest(TestCase):
	"""Pure Python: the Frappe calls are replaced."""

	def setUp(self):
		frappe.local.flags = frappe._dict()
		self.addCleanup(lambda: delattr(frappe.local, "flags"))
		self.cancelled = SimpleNamespace(
			name="PRQ-2", reference_doctype="Sales Order", reference_name="SO-1", currency="UAH"
		)
		self.order = {"amount": 45.67, "currency": "UAH"}
		self.comment = mock.Mock()
		patches = (
			mock.patch.object(frappe, "db", mock.Mock(), create=True),
			mock.patch.object(frappe, "log_error"),
			mock.patch.object(frappe, "get_doc", return_value=mock.Mock(add_comment=self.comment)),
		)
		self.db, self.log, self.get_doc = (patch.start() for patch in patches)
		for patch in patches:
			self.addCleanup(patch.stop)

	def test_a_live_request_with_the_paid_amount_takes_the_payment_and_is_linked(self):
		with mock.patch.object(
			frappe, "get_all", return_value=[{"name": "PRQ-3", "grand_total": 45.67}]
		) as rows:
			result = api._live_request(self.cancelled, self.order, "WFP-1")

		self.assertIs(result, self.get_doc.return_value)
		self.get_doc.assert_called_with("Payment Request", "PRQ-3")
		filters = rows.call_args.kwargs["filters"]
		self.assertEqual(filters["docstatus"], 1)
		self.assertEqual(filters["status"], ["in", ["Requested", "Pending"]])
		self.assertEqual((filters["reference_doctype"], filters["reference_name"]), ("Sales Order", "SO-1"))
		self.assertEqual(filters["currency"], "UAH")
		self.db.set_value.assert_called_once_with(
			"Integration Request", "WFP-1", {"reference_docname": "PRQ-3"}, update_modified=False
		)
		self.log.assert_not_called()

	def test_without_a_fitting_request_the_failure_is_written_and_nothing_is_created(self):
		with mock.patch.object(frappe, "get_all", return_value=[{"name": "PRQ-3", "grand_total": 10}]):
			result = api._live_request(self.cancelled, self.order, "WFP-1")

		self.assertIsNone(result)
		self.assertIn("book it manually", frappe.flags.wayforpay_unbooked)
		self.log.assert_called_once()
		self.assertIn("book manually", self.log.call_args.kwargs["title"])
		self.comment.assert_called_once()
		self.db.set_value.assert_not_called()
		self.get_doc.assert_called_once_with("Sales Order", "SO-1")  # only the document that gets the comment


class TestSettled(TestCase):
	def test_a_refunded_or_unbooked_payment_is_not_asked_about_again(self):
		self.assertTrue(reconcile._settled({"refunded": True}))
		self.assertTrue(reconcile._settled({"unbooked": "book it manually"}))
		self.assertFalse(reconcile._settled({"redirect_to": "/x"}))
