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
	if data.get("merchantAccount") != settings.merchant_account or not signature.notification_is_genuine(
		settings.secret_key(), data
	):
		frappe.log_error(title="WayForPay: the signature of a notification does not match", message=reference)
		frappe.local.response.http_status_code = 400
		return
	if not checkout.amount_matches(order, data):
		frappe.log_error(
			title="WayForPay: the amount of a notification differs from the order",
			message=f"{reference}: {data.get('amount')} {data.get('currency')} instead of {order.get('amount')} {order.get('currency')}",
		)
		frappe.local.response.http_status_code = 400
		return
	_record(request, order, data)
	_answer(settings, reference)


def _record(request, order: dict, data: dict):
	"""Keep the notification and, once, pay the document that the payment is for."""
	status = data.get("transactionStatus")
	output = {
		key: data.get(key) for key in ("transactionStatus", "reason", "reasonCode", "paymentSystem", "email")
	}
	if request.status == "Completed":  # a repeated notification: nothing to do twice
		return
	if status in SUCCESS:
		request.db_set("status", "Completed", update_modified=True)
		request.db_set("output", json.dumps({**output, "redirect_to": _pay(order)}))
	elif status in FAILURE:
		request.db_set("status", "Failed", update_modified=True)
		request.db_set("output", json.dumps(output))
	else:  # in process: the next notification tells the end
		request.db_set("output", json.dumps(output))
	frappe.db.commit()


def _pay(order: dict) -> str | None:
	"""Let the document know that it is paid (a Payment Request books the payment); returns where to go next."""
	doctype, name = order.get("reference_doctype"), order.get("reference_docname")
	if not (doctype and name):
		return None
	frappe.set_user(
		"Administrator"
	)  # the visitor of the notification is the gateway, the signature was checked
	try:
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
		checkout.result_url(reference) if REFERENCE.match(reference) else get_url("/payment-failed")
	)


@frappe.whitelist(allow_guest=True)
def status(order: str):
	"""Where a payment is: "Completed" (with the address to go to), "Failed" or "Pending". It shows nothing else."""
	if not REFERENCE.match(order or ""):
		return {"status": "Failed"}
	request = frappe.db.get_value(
		"Integration Request",
		{"name": order, "integration_request_service": "WayForPay"},
		["status", "output", "reference_doctype", "reference_docname"],
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
	return {"status": "Failed" if request.status in ("Failed", "Cancelled") else "Pending"}
