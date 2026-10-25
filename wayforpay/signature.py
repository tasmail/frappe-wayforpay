"""The signatures of WayForPay: HMAC-MD5 of the values joined with a semicolon, in the order of the documentation."""

import hashlib
import hmac
import json
from decimal import Decimal

PAY_URL = "https://secure.wayforpay.com/pay"
VERIFY_URL = "https://secure.wayforpay.com/verify"
API_URL = "https://api.wayforpay.com/api"
# the order of the values of the signature of a payment request, and of a notification of the result
REQUEST_FIELDS = (
	"merchantAccount",
	"merchantDomainName",
	"orderReference",
	"orderDate",
	"amount",
	"currency",
)
NOTIFICATION_FIELDS = (
	"merchantAccount",
	"orderReference",
	"amount",
	"currency",
	"authCode",
	"cardPan",
	"transactionStatus",
	"reasonCode",
)


def sign(secret: str, values) -> str:
	"""The signature of the values (each is turned into text)."""
	text = ";".join("" if value is None else str(value) for value in values)
	return hmac.new(secret.encode("utf-8"), text.encode("utf-8"), hashlib.md5).hexdigest()


def format_amount(amount) -> str:
	"""The amount as WayForPay wants it in the request: whole numbers without a fraction, else two digits."""
	value = Decimal(str(amount)).quantize(Decimal("0.01"))
	return str(int(value)) if value == value.to_integral_value() else f"{value:.2f}"


def request_values(fields: dict, products: list[dict]) -> list:
	"""The values that the signature of a payment request covers: the order, then the products."""
	return [
		*(fields[name] for name in REQUEST_FIELDS),
		*(product["name"] for product in products),
		*(product["count"] for product in products),
		*(product["price"] for product in products),
	]


def sign_request(secret: str, fields: dict, products: list[dict]) -> str:
	return sign(secret, request_values(fields, products))


def parse_notification(body: str) -> dict:
	"""The notification of the result; the numbers stay as the text that WayForPay sent (the signature needs it)."""
	data = json.loads(body, parse_float=str, parse_int=str)
	if not isinstance(data, dict):
		raise ValueError("the notification is not an object")
	return data


def notification_signature(secret: str, data: dict) -> str:
	return sign(secret, [data.get(name) for name in NOTIFICATION_FIELDS])


def notification_is_genuine(secret: str, data: dict) -> bool:
	given = str(data.get("merchantSignature") or "")
	return hmac.compare_digest(given, notification_signature(secret, data))


def answer(secret: str, order_reference: str, time: int, status: str = "accept") -> dict:
	"""What WayForPay waits for in the answer to a notification: it stops sending the notification."""
	return {
		"orderReference": order_reference,
		"status": status,
		"time": time,
		"signature": sign(secret, [order_reference, status, time]),
	}


def sign_verify(secret: str, fields: dict) -> str:
	"""The signature of a card verification: the order with a zero amount, without products."""
	return sign(
		secret,
		[
			fields[name]
			for name in ("merchantAccount", "merchantDomainName", "orderReference", "amount", "currency")
		],
	)
