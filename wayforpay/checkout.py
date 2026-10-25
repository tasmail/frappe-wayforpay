"""The payment request that the visitor's browser sends to WayForPay, made from an Integration Request."""

import json
import time
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint, flt, get_url, validate_email_address

from wayforpay import signature

LANGUAGES = {"uk": "UA", "ru": "RU", "en": "EN"}
NOTIFY_METHOD = "wayforpay.api.notify"
RETURN_METHOD = "wayforpay.api.returned"


def load_request(name: str):
	"""The Integration Request of a payment and the settings of its gateway; raises when it is not one of ours."""
	if not name or not frappe.db.exists("Integration Request", name):
		frappe.throw(_("The payment was not found"), frappe.DoesNotExistError)
	request = frappe.get_doc("Integration Request", name)
	if request.integration_request_service != "WayForPay":
		frappe.throw(_("The payment was not found"), frappe.DoesNotExistError)
	data = json.loads(request.data or "{}")
	return request, data, frappe.get_doc("WayForPay Settings", data["wayforpay_settings"])


def page_language(settings) -> str:
	if settings.language in LANGUAGES.values():
		return settings.language
	return LANGUAGES.get((frappe.local.lang or "uk")[:2], "UA")


def products_of(data: dict) -> list[dict]:
	"""One line for the whole payment: WayForPay checks that the lines add up to the amount."""
	name = data.get("description") or data.get("title") or data.get("reference_docname") or _("Payment")
	return [{"name": str(name)[:250], "count": 1, "price": signature.format_amount(data["amount"])}]


def payment_form(name: str) -> dict:
	"""{"action": the address of WayForPay, "fields": [(name, value)]} of the payment `name`."""
	request, data, settings = load_request(name)
	if request.status == "Completed":
		frappe.throw(_("This payment has been made already"))
	products = products_of(data)
	fields = {
		"merchantAccount": settings.merchant_account,
		"merchantDomainName": settings.merchant_domain_name,
		"orderReference": request.name,
		"orderDate": int(time.time()),
		"amount": signature.format_amount(data["amount"]),
		"currency": data["currency"],
	}
	secret = settings.secret_key()
	rows = [
		("merchantAccount", fields["merchantAccount"]),
		("merchantAuthType", "SimpleSignature"),
		("merchantDomainName", fields["merchantDomainName"]),
		("merchantTransactionSecureType", "AUTO"),
		("orderReference", fields["orderReference"]),
		("orderDate", fields["orderDate"]),
		("amount", fields["amount"]),
		("currency", fields["currency"]),
		("orderTimeout", cint(settings.order_timeout) or 86400),
		("language", page_language(settings)),
		("serviceUrl", get_url(f"/api/method/{NOTIFY_METHOD}")),
		("returnUrl", get_url(f"/api/method/{RETURN_METHOD}")),
	]
	if validate_email_address(
		data.get("payer_email") or ""
	):  # a payer with no address (a service user) sends none
		rows.append(("clientEmail", data["payer_email"]))
	if data.get("payer_name"):
		rows.append(("clientFirstName", str(data["payer_name"])[:60]))
	for key in ("name", "count", "price"):
		rows += [(f"product{key.capitalize()}[]", product[key]) for product in products]
	rows.append(("merchantSignature", signature.sign_request(secret, fields, products)))
	return {"action": signature.PAY_URL, "fields": rows}


def result_url(name: str) -> str:
	return get_url(f"/wayforpay_result?order={quote(name)}")


def amount_matches(data: dict, notification: dict) -> bool:
	return abs(flt(notification.get("amount")) - flt(data.get("amount"))) < 0.005 and notification.get(
		"currency"
	) == data.get("currency")
