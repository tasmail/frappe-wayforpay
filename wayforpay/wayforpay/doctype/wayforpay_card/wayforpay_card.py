from frappe.model.document import Document


class WayForPayCard(Document):
	def token(self) -> str:
		return self.get_password("rec_token", raise_exception=False) or ""
