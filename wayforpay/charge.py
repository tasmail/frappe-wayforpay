"""A charge of a saved card without the payer: CHARGE of the API of WayForPay with the token of the card."""

import time

import frappe
import requests
from frappe.integrations.utils import create_request_log
from frappe.utils import get_url

from wayforpay import api, checkout, signature
from wayforpay.reconcile import TIMEOUT
from wayforpay.wayforpay.doctype.wayforpay_settings.wayforpay_settings import SERVICE


def charge_card(card_name: str, amount, currency: str, description: str, **order) -> dict:
	"""Charge `amount` to the saved card; `order` is kept with the payment (on_paid, on_refunded, payer_*...).

	Returns {"status": the status of WayForPay, "reference": the reference of the payment, "reason": ...}. A status
	of Approved has booked the payment (on_paid) already; Declined has not; InProcessing waits for the result.
	"""
	card = frappe.get_doc("WayForPay Card", card_name)
	if card.status != "Active" or not card.token():
		return {"status": "NoCard", "reference": None, "reason": "The card is not active"}
	settings = frappe.get_doc("WayForPay Settings", card.gateway)
	request = create_request_log(
		{
			**order,
			"kind": "charge",
			"amount": amount,
			"currency": currency,
			"description": description,
			"wayforpay_settings": settings.name,
			"card": card.name,
		},
		service_name=SERVICE,
	)
	frappe.db.commit()
	data = _ask(settings, card, request.name, amount, currency, description, order)
	request, order_data, settings = checkout.load_request(request.name)
	if not data or not api.is_trustworthy(settings, order_data, data, request.name):
		return {"status": "Error", "reference": request.name, "reason": "No trustworthy answer"}
	api.record(request, order_data, data)
	return {
		"status": data.get("transactionStatus"),
		"reference": request.name,
		"reason": data.get("reason"),
	}


def _ask(settings, card, reference, amount, currency, description, payer: dict) -> dict | None:
	fields = {
		"merchantAccount": settings.merchant_account,
		"merchantDomainName": settings.merchant_domain_name,
		"orderReference": reference,
		"orderDate": int(time.time()),
		"amount": signature.format_amount(amount),
		"currency": currency,
	}
	product = {"name": str(description)[:250], "count": 1, "price": fields["amount"]}
	name = str(payer.get("payer_name") or "Client")
	body = {
		"transactionType": "CHARGE",
		**fields,
		"merchantAuthType": "SimpleSignature",
		"merchantTransactionType": "SALE",
		"merchantTransactionSecureType": "AUTO",
		"productName": [product["name"]],
		"productCount": [product["count"]],
		"productPrice": [product["price"]],
		"clientFirstName": name[:60],
		"clientLastName": name[:60],
		"clientEmail": payer.get("payer_email") or "",
		"clientPhone": payer.get("payer_phone") or "380000000000",
		"clientCountry": "UKR",
		"recToken": card.token(),
		"apiVersion": 1,
		"serviceUrl": get_url(f"/api/method/{checkout.NOTIFY_METHOD}"),
		"merchantSignature": signature.sign_request(settings.secret_key(), fields, [product]),
	}
	response = requests.post(signature.API_URL, json=body, timeout=TIMEOUT)
	response.raise_for_status()
	data = signature.parse_notification(response.text)
	return data if data.get("transactionStatus") else None
