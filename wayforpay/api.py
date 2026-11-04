"""The endpoints that WayForPay and the browser of the payer call."""

import json
import re
import time

import frappe
from frappe.utils import flt, get_url

from wayforpay import checkout, signature

SUCCESS = ("Approved",)
FAILURE = ("Declined", "Expired")
# The states of a submitted Payment Request that can still be paid. ERPNext sets Initiated when the request has a payment
# url (the case of every request made for a gateway account) and Partially Paid when a part of it was paid; both are
# payable like Requested, for what is still outstanding.
LIVE_STATUSES = ["Requested", "Initiated", "Partially Paid"]
# The keys of the output of an Integration Request that tell a manager what happened to a payment.
AUDIT = ("unbooked", "cancelled_request")
AMOUNT_TOLERANCE = 0.005
REFERENCE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _answer(settings, reference: str):
	"""The body of the answer is the JSON that WayForPay waits for, not the usual envelope of Frappe."""
	frappe.local.message_log = []  # the messages of the booking (Payment Entry created...) are not for the gateway
	frappe.local.response.update(signature.answer(settings.secret_key(), reference, int(time.time())))


@frappe.whitelist(allow_guest=True, methods=["POST"])
def notify():
	"""WayForPay tells the result of a payment (serviceUrl). Nothing is believed before the signature matches."""
	try:
		data = signature.parse_notification(frappe.request.get_data(as_text=True))
		reference = str(data.get("orderReference") or "")
		if not REFERENCE.match(reference):
			raise ValueError("bad reference")
		request, order, settings = checkout.load_request(reference)
	except Exception:
		frappe.local.response.http_status_code = 400
		return
	if not is_trustworthy(settings, order, data, reference):
		frappe.local.response.http_status_code = 400
		return
	record(request, order, data)
	_answer(settings, reference)


def is_trustworthy(settings, order: dict, data: dict, reference: str) -> bool:
	"""The result of a payment is believed when it is signed with the key of the merchant and is for this order."""
	if data.get("merchantAccount") != settings.merchant_account or not signature.notification_is_genuine(
		settings.secret_key(), data
	):
		frappe.log_error(title="WayForPay: the signature of a result does not match", message=reference)
		return False
	if not checkout.amount_matches(order, data):
		frappe.log_error(
			title="WayForPay: the amount of a result differs from the order",
			message=f"{reference}: {data.get('amount')} {data.get('currency')} instead of {order.get('amount')}",
		)
		return False
	return True


def record(request, order: dict, data: dict):
	"""Keep the notification and, once, pay the document that the payment is for."""
	status = data.get("transactionStatus")
	output = {
		key: data.get(key) for key in ("transactionStatus", "reason", "reasonCode", "paymentSystem", "email")
	}
	if order.get("kind") == "verify":
		return _record_card(request, order, data, output)
	if request.status == "Completed":  # a repeated notification: nothing to pay twice
		if status == "Refunded":
			_refund(request, order, data)
		return
	if status in SUCCESS:
		request.db_set("status", "Completed", update_modified=True)
		redirect = _pay(order, request.name)
		# what _pay wrote about a payment it could not book (or booked elsewhere) stays in the output
		kept = _stored_output(request.name)
		audit = {key: kept[key] for key in AUDIT if key in kept}
		request.db_set("output", json.dumps({**output, "redirect_to": redirect, **audit}))
	elif status in FAILURE:
		request.db_set("status", "Failed", update_modified=True)
		request.db_set("output", json.dumps(output))
	else:  # in process: the next notification tells the end
		request.db_set("output", json.dumps(output))
	frappe.db.commit()


def _sibling(path: str | None, name: str) -> str | None:
	"""The function `name` that lives next to the function `path`, when there is one (orders made before refunds)."""
	if not path or "." not in path:
		return None
	module, _function = path.rsplit(".", 1)
	try:
		return f"{module}.{name}" if hasattr(frappe.get_module(module), name) else None
	except ImportError:
		return None


def _record_card(request, order: dict, data: dict, output: dict):
	"""The result of a card verification: a verified card is saved (its token never goes into the log)."""
	if request.status == "Completed":
		return
	if data.get("transactionStatus") in SUCCESS and data.get("recToken"):
		from wayforpay import cards

		card = cards.save_card(order, data)
		redirect = None
		handler = order.get("on_card")
		if handler:
			frappe.set_user("Administrator")
			try:
				redirect = frappe.get_attr(handler)(order, card)
			except Exception:
				frappe.log_error(
					title="WayForPay: a saved card was not told to the shop", message=frappe.get_traceback()
				)
		request.db_set("status", "Completed", update_modified=True)
		request.db_set(
			"output",
			json.dumps({**output, "cardPan": data.get("cardPan"), "card": card, "redirect_to": redirect}),
		)
	elif data.get("transactionStatus") in FAILURE:
		request.db_set("status", "Failed", update_modified=True)
		request.db_set("output", json.dumps(output))
	else:
		request.db_set("output", json.dumps(output))
	frappe.db.commit()


def _refund(request, order: dict, data: dict):
	"""Tell the shop, once, that a payment that it booked was returned to the payer."""
	output = json.loads(request.output or "null") or {}
	if output.get("refunded"):
		return
	handler = order.get("on_refunded") or _sibling(order.get("on_paid"), "on_refunded")
	if handler:
		frappe.set_user("Administrator")
		try:
			frappe.get_attr(handler)(order, request.name, data)
		except Exception:
			frappe.log_error(title="WayForPay: a refund was not booked", message=frappe.get_traceback())
			return
	request.db_set("output", json.dumps({**output, "refunded": True}))
	frappe.db.commit()


def _pay(order: dict, reference: str = "") -> str | None:
	"""Let the shop know that the order is paid; returns where to go next.

	The code that started the payment can name a function in the data of the order (`on_paid`, a dotted path;
	the data is kept on the server, the payer cannot change it): it is called with the order and the reference
	of the payment. Otherwise the document of the order (a Payment Request books the payment) is told.
	"""
	handler = order.get("on_paid")
	doctype, name = order.get("reference_doctype"), order.get("reference_docname")
	if not (handler or (doctype and name)):
		return None
	# the visitor of the notification is the gateway, the signature was checked
	frappe.set_user("Administrator")
	try:
		if handler:
			return frappe.get_attr(handler)(order, reference)
		document = frappe.get_doc(doctype, name)
		if doctype == "Payment Request" and document.docstatus == 2:
			return _pay_cancelled(document, order, reference)
		return document.run_method("on_payment_authorized", "Completed")
	except Exception:
		frappe.log_error(title="WayForPay: the paid document was not updated", message=frappe.get_traceback())
		_note(reference, unbooked="the paid document was not updated, see the Error Log: book it manually")
		return None


def _stored_output(reference: str) -> dict:
	return json.loads(frappe.db.get_value("Integration Request", reference, "output") or "null") or {}


def _note(reference: str, **audit):
	"""Write what happened to a payment into the output of its Integration Request (kept by `record`).

	A failure here is logged and never raised: it must not hide the failure that is being reported.
	"""
	try:
		frappe.db.set_value(
			"Integration Request",
			reference,
			"output",
			json.dumps({**_stored_output(reference), **audit}),
			update_modified=False,
		)
	except Exception:
		frappe.log_error(
			title="WayForPay: the note on a payment was not written", message=frappe.get_traceback()
		)


def booked_amount(candidate) -> float:
	"""What booking a Payment Request pays: its outstanding amount (ERPNext books that), else its total.

	`candidate` is a dict of a query or a Payment Request.
	"""
	outstanding = candidate.get("outstanding_amount")
	return flt(outstanding if outstanding is not None else candidate.get("grand_total"))


def _fits(live, order: dict) -> bool:
	return abs(booked_amount(live) - flt(order.get("amount"))) < AMOUNT_TOLERANCE


def pick_live(candidates: list[dict], amount: float) -> str | None:
	"""The name of the request that takes a payment of `amount`: the newest whose booked amount equals it, or None.

	`candidates` are the live requests of the document (newest first), each a dict with `name`, `grand_total`
	and `outstanding_amount`.
	"""
	for candidate in candidates:
		if abs(booked_amount(candidate) - flt(amount)) < AMOUNT_TOLERANCE:
			return candidate["name"]
	return None


def _pay_cancelled(cancelled, order: dict, reference: str) -> str | None:
	"""Book a payment made for a cancelled Payment Request on the live request of the same document.

	The shop cancels the open request of an order when the customer asks to pay again, and the customer may still
	pay on the first page of the gateway. The money is real, so it goes to the live request that fits (see
	_live_request). The Integration Request is linked to that request only after the booking has succeeded, and keeps
	the name of the cancelled one. When nothing fits, or the booking raises, the payment is marked `unbooked`.
	"""
	live = _live_request(cancelled, order, reference)
	if not live:
		return None
	# the request may have been paid since it was chosen (a payment on its own page): read it again right before
	live.reload()
	if live.docstatus != 1 or live.status not in LIVE_STATUSES or not _fits(live, order):
		_unbooked(
			cancelled,
			reference,
			f"Payment {reference} was received for the cancelled request {cancelled.name}, and the live request "
			f"{live.name} is {live.status} with {live.get('outstanding_amount')} outstanding, so nothing was booked: "
			"check the existing Payment Entries and book it manually.",
		)
		return None
	try:
		redirect = live.run_method("on_payment_authorized", "Completed")
	except Exception:
		frappe.log_error(title="WayForPay: the paid document was not updated", message=frappe.get_traceback())
		_unbooked(
			cancelled,
			reference,
			f"Payment {reference} was received for the cancelled request {cancelled.name} and booking it on "
			f"{live.name} failed (see the Error Log). The booking may have been partly done: check the existing "
			"Payment Entries of the document before booking it manually.",
		)
		return None
	frappe.db.set_value(
		"Integration Request", reference, "reference_docname", live.name, update_modified=False
	)
	_note(reference, cancelled_request=cancelled.name)
	return redirect


def _unbooked(cancelled, reference: str, message: str):
	"""Make a payment that nothing booked visible: the output of its Integration Request, the Error Log and a comment."""
	_note(reference, unbooked=message, cancelled_request=cancelled.name)
	frappe.log_error(
		title="WayForPay: payment received for a cancelled request, book manually",
		message=message,
		reference_doctype=cancelled.reference_doctype,
		reference_name=cancelled.reference_name,
	)
	try:
		frappe.get_doc(cancelled.reference_doctype, cancelled.reference_name).add_comment("Comment", message)
	except Exception:
		frappe.log_error(
			title="WayForPay: the comment on the document was not added", message=frappe.get_traceback()
		)


def _live_request(cancelled, order: dict, reference: str):
	"""The live Payment Request that takes a payment made for a cancelled one, or None (the failure is then written).

	Live means: the same document, docstatus 1, an Inward request that is not a Phone one, status Requested,
	Initiated or Partially Paid, the same currency, and a booked amount (outstanding amount) equal to the paid
	amount. Nothing is created or cancelled here.
	"""
	candidates = frappe.get_all(
		"Payment Request",
		filters={
			"reference_doctype": cancelled.reference_doctype,
			"reference_name": cancelled.reference_name,
			"docstatus": 1,
			"status": ["in", LIVE_STATUSES],
			"currency": cancelled.currency,
			# an Outward request (a payment to a supplier) and a Phone one (its payment books nothing) are not it
			"payment_request_type": "Inward",
			"payment_channel": ["!=", "Phone"],
		},
		fields=["name", "grand_total", "outstanding_amount"],
		order_by="creation desc",
	)
	name = pick_live(candidates, flt(order.get("amount")))
	if name:
		return frappe.get_doc("Payment Request", name)
	_unbooked(
		cancelled,
		reference,
		f"Payment {reference} of {order.get('amount')} {order.get('currency')} was received for the cancelled "
		f"request {cancelled.name}, and no live request of {cancelled.reference_doctype} "
		f"{cancelled.reference_name} fits: book it manually.",
	)
	return None


def _keep_cookies_of_the_browser():
	"""Make the answer set no cookie at all.

	The POST of the gateway is cross-site, so the browser leaves the `sid` cookie (SameSite=Lax) out of it and Frappe
	serves the request as Guest. The cookie manager then writes `sid=Guest`, `user_id=Guest`, `full_name`, `user_image`
	and `system_user` into the answer, which would replace the real cookies of a logged in payer. Nothing adds them
	again after the endpoint: the cookies are written by `process_response` from this very manager.
	"""
	manager = getattr(frappe.local, "cookie_manager", None)
	if manager:
		manager.cookies.clear()
		del manager.to_delete[:]


@frappe.whitelist(allow_guest=True)
def returned():
	"""The payer comes back from WayForPay (returnUrl, a POST): the page of the result waits for the notification."""
	reference = str(frappe.form_dict.get("orderReference") or "")
	_keep_cookies_of_the_browser()
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = (
		checkout.result_url(reference) if REFERENCE.match(reference) else get_url("/")
	)


@frappe.whitelist(allow_guest=True)
def status(order: str):
	"""Where a payment is: "Completed" (with the address to go to), "Failed" or "Pending". It shows nothing else."""
	if not REFERENCE.match(order or ""):
		return {"status": "Failed"}
	request = frappe.db.get_value(
		"Integration Request",
		{"name": order, "integration_request_service": "WayForPay"},
		["status", "output", "reference_doctype", "reference_docname", "data"],
		as_dict=True,
	)
	if not request:
		return {"status": "Failed"}
	if request.status == "Completed":
		redirect = (json.loads(request.output or "{}")).get("redirect_to")
		return {
			"status": "Completed",
			"redirect_to": redirect
			or f"/payment-success?doctype={request.reference_doctype}&docname={request.reference_docname}",
		}
	if request.status in ("Failed", "Cancelled"):
		# the code that started the payment can say where a payer goes after a refusal (a site with no shop pages)
		return {"status": "Failed", "redirect_to": json.loads(request.data or "{}").get("failed_redirect")}
	return {"status": "Pending"}
