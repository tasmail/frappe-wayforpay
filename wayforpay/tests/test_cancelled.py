import json
from types import SimpleNamespace
from unittest import TestCase, mock

import frappe

from wayforpay import api, reconcile


def row(name, total=45.67, outstanding=None):
	return {
		"name": name,
		"grand_total": total,
		"outstanding_amount": total if outstanding is None else outstanding,
	}


class TestPickLive(TestCase):
	"""Pure Python: the choice of the request that takes a payment made for a cancelled one."""

	def test_the_newest_request_with_the_paid_amount_is_taken(self):
		self.assertEqual(api.pick_live([row("PRQ-3"), row("PRQ-2")], 45.67), "PRQ-3")

	def test_the_amount_must_match_within_the_tolerance(self):
		self.assertEqual(api.pick_live([row("PRQ-3")], 45.674), "PRQ-3")
		self.assertIsNone(api.pick_live([row("PRQ-3")], 45.68))
		self.assertIsNone(api.pick_live([row("PRQ-3")], 10))

	def test_the_outstanding_amount_is_what_booking_pays(self):
		part_paid = row("PRQ-3", total=100, outstanding=45.67)
		self.assertEqual(api.pick_live([part_paid], 45.67), "PRQ-3")
		self.assertIsNone(api.pick_live([part_paid], 100))

	def test_a_request_with_no_outstanding_amount_is_compared_by_its_total(self):
		self.assertEqual(api.pick_live([{"name": "PRQ-3", "grand_total": 45.67}], 45.67), "PRQ-3")

	def test_no_request_no_choice(self):
		self.assertIsNone(api.pick_live([], 45.67))

	def test_the_open_states_are_real_states_of_erpnext(self):
		self.assertEqual(api.LIVE_STATUSES, ["Requested", "Initiated"])


class Doubles(TestCase):
	"""The Frappe calls are replaced: no site is needed and nothing global is touched (the patches are undone)."""

	def setUp(self):
		self.cancelled = SimpleNamespace(
			name="PRQ-2", reference_doctype="Sales Order", reference_name="SO-1", currency="UAH"
		)
		self.order = {"amount": 45.67, "currency": "UAH"}
		self.comment = mock.Mock()
		self.stored = {}
		self.live = mock.Mock()
		self.live.name = "PRQ-3"
		self.live.run_method.return_value = "/orders/1"

		def get_doc(doctype, name=None):
			return self.live if doctype == "Payment Request" else mock.Mock(add_comment=self.comment)

		db = mock.Mock()
		db.get_value.side_effect = lambda *args, **kw: json.dumps(self.stored) if self.stored else None
		db.set_value.side_effect = self.set_value
		patches = (
			mock.patch.object(frappe, "db", db, create=True),
			mock.patch.object(frappe, "log_error"),
			mock.patch.object(frappe, "get_traceback", return_value="traceback"),
			mock.patch.object(frappe, "get_doc", side_effect=get_doc),
		)
		self.db, self.log, _traceback, self.get_doc = (patch.start() for patch in patches)
		for patch in patches:
			self.addCleanup(patch.stop)

	def set_value(self, doctype, name, field, value=None, **kw):
		if field == "output":
			self.stored = json.loads(value)

	def linked(self):
		return [call.args for call in self.db.set_value.call_args_list if call.args[2] == "reference_docname"]


class TestLiveRequest(Doubles):
	def test_the_query_asks_for_the_live_requests_of_the_same_document_and_currency(self):
		with mock.patch.object(frappe, "get_all", return_value=[row("PRQ-3")]) as rows:
			result = api._live_request(self.cancelled, self.order, "WFP-1")

		self.assertIs(result, self.live)
		filters = rows.call_args.kwargs["filters"]
		self.assertEqual(filters["docstatus"], 1)
		self.assertEqual(filters["status"], ["in", ["Requested", "Initiated"]])
		self.assertEqual((filters["reference_doctype"], filters["reference_name"]), ("Sales Order", "SO-1"))
		self.assertEqual(filters["currency"], "UAH")
		self.assertIn("outstanding_amount", rows.call_args.kwargs["fields"])
		self.assertEqual(self.linked(), [])  # nothing is linked before the booking has succeeded

	def test_without_a_fitting_request_the_failure_is_written_everywhere(self):
		with mock.patch.object(frappe, "get_all", return_value=[row("PRQ-3", 10)]):
			result = api._live_request(self.cancelled, self.order, "WFP-1")

		self.assertIsNone(result)
		self.assertIn("book it manually", self.stored["unbooked"])
		self.assertEqual(self.stored["cancelled_request"], "PRQ-2")
		self.assertIn("book manually", self.log.call_args.kwargs["title"])
		self.comment.assert_called_once()
		self.live.run_method.assert_not_called()


class TestPayCancelled(Doubles):
	def pay(self, candidates):
		with mock.patch.object(frappe, "get_all", return_value=candidates):
			return api._pay_cancelled(self.cancelled, self.order, "WFP-1")

	def test_a_booked_payment_is_linked_to_the_live_request_and_keeps_the_cancelled_name(self):
		self.assertEqual(self.pay([row("PRQ-3")]), "/orders/1")

		self.live.run_method.assert_called_once_with("on_payment_authorized", "Completed")
		self.assertEqual(self.linked(), [("Integration Request", "WFP-1", "reference_docname", "PRQ-3")])
		self.assertEqual(self.stored, {"cancelled_request": "PRQ-2"})

	def test_a_booking_that_raises_links_nothing_and_marks_the_payment(self):
		self.live.run_method.side_effect = frappe.ValidationError("cannot book")

		self.assertIsNone(self.pay([row("PRQ-3")]))

		self.assertEqual(self.linked(), [])
		self.assertIn("failed", self.stored["unbooked"])
		self.assertEqual(self.stored["cancelled_request"], "PRQ-2")
		self.log.assert_called()
		self.comment.assert_called_once()

	def test_the_audit_survives_the_output_that_record_writes(self):
		self.stored = {"unbooked": "x", "cancelled_request": "PRQ-2", "other": 1}

		kept = api._stored_output("WFP-1")

		self.assertEqual(
			{key: kept[key] for key in api.AUDIT if key in kept},
			{"unbooked": "x", "cancelled_request": "PRQ-2"},
		)


class TestSettled(TestCase):
	def test_a_refunded_or_unbooked_payment_is_not_asked_about_again(self):
		self.assertTrue(reconcile._settled({"refunded": True}))
		self.assertTrue(reconcile._settled({"unbooked": "book it manually"}))
		self.assertFalse(reconcile._settled({"redirect_to": "/x"}))
