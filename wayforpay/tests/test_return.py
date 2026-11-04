from unittest import TestCase, mock

import frappe
from frappe.auth import CookieManager
from werkzeug.wrappers import Response

from wayforpay import api

SESSION_COOKIES = ("sid", "user_id", "full_name", "user_image", "system_user")


class TestReturn(TestCase):
	"""Pure Python: no site is needed, the cookie manager of Frappe is used as it is."""

	def setUp(self):
		self.manager = CookieManager()
		frappe.local.cookie_manager = self.manager
		frappe.local.response = frappe._dict()
		patches = (
			mock.patch.object(frappe, "form_dict", frappe._dict(orderReference="WFP-1"), create=True),
			mock.patch.object(
				api.checkout, "result_url", return_value="https://shop.example/wayforpay_result?order=WFP-1"
			),
			mock.patch.object(api, "get_url", return_value="https://shop.example/"),
		)
		for patch in patches:
			patch.start()
			self.addCleanup(patch.stop)
		self.addCleanup(lambda: delattr(frappe.local, "cookie_manager"))

	def guest(self):
		"""What LoginManager does for a request without the cookie of the session."""
		for key in ("sid", "user_id", "full_name", "user_image", "system_user"):
			self.manager.set_cookie(key, "Guest")
		self.manager.delete_cookie("preferred_language")

	def flushed(self):
		response = Response()
		self.manager.flush_cookies(response=response)
		return response.headers.getlist("Set-Cookie")

	def test_a_guest_answer_would_replace_the_cookies_of_the_browser(self):
		self.guest()
		self.assertEqual(len(self.flushed()), len(SESSION_COOKIES) + 1)  # the premise of the fix

	def test_the_redirect_of_the_return_sets_no_cookie(self):
		self.guest()
		api.returned()

		self.assertEqual(frappe.local.response["type"], "redirect")
		self.assertEqual(
			frappe.local.response["location"], "https://shop.example/wayforpay_result?order=WFP-1"
		)
		self.assertEqual(self.flushed(), [])

	def test_a_bad_reference_goes_home_and_sets_no_cookie(self):
		self.guest()
		frappe.form_dict["orderReference"] = "bad reference!"
		api.returned()

		self.assertEqual(frappe.local.response["location"], "https://shop.example/")
		self.assertEqual(self.flushed(), [])
