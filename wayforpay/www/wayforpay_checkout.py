import frappe
from frappe import _

from wayforpay import checkout

no_cache = 1


def get_context(context):
	context.no_cache = 1
	context.title = _("Payment")
	try:
		context.form = checkout.payment_form(frappe.form_dict.get("order"))
	except Exception as error:
		frappe.local.response.http_status_code = 400
		context.form = None
		context.error = (
			str(error) if isinstance(error, frappe.ValidationError) else _("The payment cannot be started")
		)
