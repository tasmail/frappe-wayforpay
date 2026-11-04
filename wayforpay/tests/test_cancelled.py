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

	def test_only_the_states_that_can_still_be_paid_are_live(self):
		for status in ("Requested", "Initiated", "Partially Paid"):
			self.assertIn(status, api.LIVE_STATUSES)
		for status in ("Draft", "Paid", "Cancelled", "Failed", "Payment Ordered", "Pending"):
			self.assertNotIn(status, api.LIVE_STATUSES)


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
		self.live.run_method.side_effect = self.book
		self.live.docstatus, self.live.status, self.live.outstanding_amount = 1, "Requested", 45.67
		self.live.get.side_effect = lambda key, default=None: getattr(self.live, key, default)

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

	def book(self, *args):
		"""What on_payment_authorized does to a request that it books: the request is paid."""
		self.live.status, self.live.outstanding_amount = "Paid", 0
		return "/orders/1"

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
		self.assertEqual(filters["status"], ["in", api.LIVE_STATUSES])
		self.assertEqual(filters["payment_request_type"], "Inward")
		self.assertEqual(filters["payment_channel"], ["!=", "Phone"])
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

	def test_a_request_that_was_paid_since_it_was_chosen_is_not_booked_again(self):
		def paid_meanwhile():
			self.live.status, self.live.outstanding_amount = "Paid", 0

		self.live.reload.side_effect = paid_meanwhile

		self.assertIsNone(self.pay([row("PRQ-3")]))

		self.live.run_method.assert_not_called()
		self.assertEqual(self.linked(), [])
		self.assertIn("check the existing Payment Entries", self.stored["unbooked"])
		self.assertEqual(self.stored["cancelled_request"], "PRQ-2")

	def test_a_request_that_is_no_longer_submitted_or_owes_another_amount_is_not_booked(self):
		for change in ({"docstatus": 2}, {"outstanding_amount": 10}):
			self.stored = {}
			self.live.docstatus, self.live.status, self.live.outstanding_amount = 1, "Requested", 45.67
			self.live.reload.side_effect = lambda change=change: [
				setattr(self.live, key, value) for key, value in change.items()
			]

			self.assertIsNone(self.pay([row("PRQ-3")]), change)

			self.live.run_method.assert_not_called()
			self.assertIn("nothing was booked", self.stored["unbooked"])
		self.assertEqual(self.linked(), [])

	def test_a_booking_that_returns_without_a_payment_entry_is_not_linked(self):
		self.live.run_method.side_effect = lambda *args: "/orders/1"  # a hook that books nothing

		self.assertIsNone(self.pay([row("PRQ-3")]))

		self.assertEqual(self.linked(), [])
		self.assertIn("returned without a Payment Entry", self.stored["unbooked"])
		self.assertEqual(self.stored["cancelled_request"], "PRQ-2")
		self.comment.assert_called_once()

	def test_a_booking_that_only_lowers_the_outstanding_amount_counts_as_made(self):
		def part(*args):
			self.live.status, self.live.outstanding_amount = "Partially Paid", 20
			return "/orders/1"

		self.live.run_method.side_effect = part

		self.assertEqual(self.pay([row("PRQ-3")]), "/orders/1")
		self.assertEqual(self.linked(), [("Integration Request", "WFP-1", "reference_docname", "PRQ-3")])

	def test_a_booking_that_raises_is_rolled_back_to_the_savepoint(self):
		self.live.run_method.side_effect = frappe.ValidationError("cannot book")

		self.assertIsNone(self.pay([row("PRQ-3")]))

		self.db.savepoint.assert_called_once_with(api.SAVEPOINT)
		self.db.rollback.assert_called_once_with(save_point=api.SAVEPOINT)
		self.assertIn("rolled back", self.stored["unbooked"])
		self.assertNotIn("partly done", self.stored["unbooked"])

	def test_a_rollback_that_fails_says_that_the_booking_may_be_partly_done(self):
		self.live.run_method.side_effect = frappe.ValidationError("cannot book")
		self.db.rollback.side_effect = frappe.ValidationError("no savepoint")

		self.assertIsNone(self.pay([row("PRQ-3")]))

		self.assertIn("may be partly done", self.stored["unbooked"])

	def test_a_failure_to_link_a_booked_payment_is_not_a_failure_to_book(self):
		self.db.set_value.side_effect = frappe.ValidationError("db is gone")

		self.assertEqual(self.pay([row("PRQ-3")]), "/orders/1")  # the payment is booked and stays so

		self.db.rollback.assert_not_called()
		self.comment.assert_not_called()  # no "book it manually" about a payment that is booked
		self.assertNotIn("unbooked", self.stored)
		self.assertIn("not linked", self.log.call_args.kwargs["title"])

	def test_a_partly_paid_request_is_booked_for_what_is_outstanding(self):
		self.live.status, self.live.outstanding_amount = "Partially Paid", 45.67

		self.assertEqual(self.pay([row("PRQ-3", total=100, outstanding=45.67)]), "/orders/1")

	def test_a_failed_note_never_hides_the_failure_it_reports(self):
		self.db.set_value.side_effect = frappe.ValidationError("db is gone")

		api._note("WFP-1", unbooked="x")  # logs, does not raise

		self.log.assert_called()

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
