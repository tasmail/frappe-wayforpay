"""The endpoints that WayForPay and the browser of the payer call."""

import json
import re
import time

import frappe
from frappe.utils import get_url

from wayforpay import checkout, signature

SUCCESS = ("Approved",)
FAILURE = ("Declined", "Expired")
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
		request.db_set("output", json.dumps({**output, "redirect_to": _pay(order, request.name)}))
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
		return frappe.get_doc(doctype, name).run_method("on_payment_authorized", "Completed")
	except Exception:
		frappe.log_error(title="WayForPay: the paid document was not updated", message=frappe.get_traceback())
		return None


@frappe.whitelist(allow_guest=True)
def returned():
	"""The payer comes back from WayForPay (returnUrl, a POST): the page of the result waits for the notification."""
	reference = str(frappe.form_dict.get("orderReference") or "")
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
