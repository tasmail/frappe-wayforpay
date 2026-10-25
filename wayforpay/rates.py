"""The official rate of the National Bank of Ukraine, for a payment that is priced in a foreign currency."""

import datetime

import frappe
import requests

URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange"
CACHE_SECONDS = 3600
TIMEOUT = 20


def parse(rows, currency: str) -> tuple[float, datetime.date]:
	row = next((r for r in rows or [] if isinstance(r, dict) and r.get("cc") == currency), None)
	if not row or not row.get("rate") or not row.get("exchangedate"):
		raise ValueError(f"the National Bank sent no rate of {currency}")
	rate = float(row["rate"])
	if not 0 < rate < 1000:
		raise ValueError(f"the rate of {currency} is not credible: {rate}")
	return rate, datetime.datetime.strptime(row["exchangedate"], "%d.%m.%Y").date()


def rate(currency: str = "USD") -> float:
	"""Hryvnia for one unit of the currency; the answer is kept for an hour, the last known rate stands in for a failure."""
	key = f"wayforpay_rate_{currency}"
	cached = frappe.cache().get_value(key)
	if cached:
		return float(cached)
	last = f"{key}_last"
	try:
		response = requests.get(URL, params={"valcode": currency, "json": ""}, timeout=TIMEOUT)
		response.raise_for_status()
		value, _date = parse(response.json(), currency)
	except (requests.RequestException, ValueError):
		stale = frappe.cache().get_value(last)
		if not stale:
			raise
		return float(stale)
	frappe.cache().set_value(key, value, expires_in_sec=CACHE_SECONDS)
	frappe.cache().set_value(last, value)
	return value
