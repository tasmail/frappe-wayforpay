import json

import frappe
from frappe import _

no_cache = 1


def get_context(context):
	context.no_cache = 1
	context.title = _("Payment")
	context.order = frappe.form_dict.get("order") or ""
	context.texts = json.dumps(
		{
			"waiting": _("We are waiting for the confirmation of the bank…"),
			"late": _(
				"The payment is being processed. When it is confirmed you will get a letter; you can close this page."
			),
		}
	)
