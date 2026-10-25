"""Cards that a payer has saved at WayForPay: the token of a verified card, kept for charges without the payer."""

import frappe
from frappe.utils.password import remove_encrypted_password

DOCTYPE = "WayForPay Card"


def active_card(owner_doctype: str, owner_name: str) -> str | None:
	"""The name of the active card of the owner, or None."""
	return frappe.db.get_value(
		DOCTYPE,
		{"owner_doctype": owner_doctype, "owner_name": owner_name, "status": "Active"},
		"name",
	)


def save_card(order: dict, data: dict) -> str:
	"""Keep the token of a verified card as the active card of the owner; an earlier card is removed."""
	owner_doctype, owner_name = order["reference_doctype"], order["reference_docname"]
	remove_cards(owner_doctype, owner_name)
	card = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"gateway": order["wayforpay_settings"],
			"owner_doctype": owner_doctype,
			"owner_name": owner_name,
			"status": "Active",
			"card_pan": data.get("cardPan"),
			"card_type": data.get("cardType"),
			"issuer_bank": data.get("issuerBankName"),
			"rec_token": data["recToken"],
		}
	).insert(ignore_permissions=True)
	return card.name


def remove_cards(owner_doctype: str, owner_name: str) -> int:
	"""The owner has no active card any more: the token is forgotten, the record stays as the history."""
	names = frappe.get_all(
		DOCTYPE,
		{"owner_doctype": owner_doctype, "owner_name": owner_name, "status": "Active"},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value(DOCTYPE, name, "status", "Removed")
		remove_encrypted_password(DOCTYPE, name, "rec_token")
	return len(names)
