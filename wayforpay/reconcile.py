"""The safety net of the notifications: ask WayForPay for the state of a payment that stayed unanswered.

A notification can be lost (the site was down, an address was refused). A payment that is still queued after a few
minutes is asked about (CHECK_STATUS); the answer is signed like a notification and goes the same way.
"""

import json
from datetime import timedelta

import frappe
import requests
from frappe.utils import flt

from wayforpay import api, checkout, signature

API_URL = "https://api.wayforpay.com/api"
WAIT_MINUTES = 10  # a notification that is a few minutes late is not lost
KEEP_DAYS = 3
TIMEOUT = 30


def check_status(settings, reference: str) -> dict | None:
	"""The signed state of the payment `reference` at WayForPay; None when WayForPay knows nothing of it."""
	body = {
		"transactionType": "CHECK_STATUS",
		"merchantAccount": settings.merchant_account,
		"orderReference": reference,
		"merchantSignature": signature.sign(settings.secret_key(), [settings.merchant_account, reference]),
		"apiVersion": 1,
	}
	response = requests.post(API_URL, json=body, timeout=TIMEOUT)
	response.raise_for_status()
	data = signature.parse_notification(response.text)
	return data if data.get("transactionStatus") else None


def reconcile_payment(name: str) -> str | None:
	"""Ask about one queued payment and book the answer like a notification; returns the status that was found."""
	request, order, settings = checkout.load_request(name)
	data = check_status(settings, name)
	if not data or not api.is_trustworthy(settings, order, data, name):
		return None
	if returned_in_full(order, data):
		# the answer for a returned payment still says Approved: the refund is told by refundAmount
		data = {**data, "transactionStatus": "Refunded"}
	api.record(request, order, data)
	return data["transactionStatus"]


def returned_in_full(order: dict, data: dict) -> bool:
	return data.get("transactionStatus") == "Approved" and flt(data.get("refundAmount")) >= flt(
		order.get("amount")
	)


def reconcile() -> dict:
	"""The job of the scheduler: every payment that waits for its notification is asked about."""
	now = frappe.utils.now_datetime()
	# a payment that waits for its notification, and a payment that was booked and may have been returned since
	rows = frappe.get_all(
		"Integration Request",
		filters={
			"integration_request_service": "WayForPay",
			"status": ["in", ["Queued", "Completed"]],
			"creation": ["between", [now - timedelta(days=KEEP_DAYS), now - timedelta(minutes=WAIT_MINUTES)]],
		},
		fields=["name", "output"],
	)
	rows = [row.name for row in rows if not (json.loads(row.output or "null") or {}).get("refunded")]
	found = {}
	for name in rows:
		try:
			status = reconcile_payment(name)
		except (requests.RequestException, ValueError, json.JSONDecodeError):
			frappe.log_error(title="WayForPay: the state of a payment was not asked", message=name)
			continue
		if status:
			found[name] = status
	return found
