import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url
from payments.utils import create_payment_gateway

SERVICE = "WayForPay"


class WayForPaySettings(Document):
	supported_currencies = ("UAH", "USD", "EUR")

	def on_update(self):
		name = f"{SERVICE}-{self.gateway_name}"
		create_payment_gateway(name, settings="WayForPay Settings", controller=self.gateway_name)
		call_hook_method("payment_gateway_enabled", gateway=name)

	def secret_key(self) -> str:
		return self.get_password("merchant_secret_key", raise_exception=False) or ""

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. WayForPay does not support transactions in currency '{0}'"
				).format(currency)
			)

	def get_payment_url(self, **kwargs):
		"""The address of the page that sends the visitor to WayForPay.

		The amount, the currency and the payer are kept on the server in an Integration Request, the address
		carries only its name, so nobody can change what is paid by editing the address.
		"""
		request = create_request_log({**kwargs, "wayforpay_settings": self.name}, service_name=SERVICE)
		return get_url(f"/wayforpay_checkout?order={request.name}")
