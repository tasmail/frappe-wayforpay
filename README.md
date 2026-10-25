# wayforpay

[WayForPay](https://wayforpay.com) payment gateway for the Frappe `payments` app: a Payment Request (ERPNext,
Webshop) can be paid through it like through Stripe.

- **WayForPay Settings** (one per merchant): merchant account, merchant domain (as registered in WayForPay) and the
  secret key. Saving it creates the Payment Gateway `WayForPay-<name>` and its Payment Gateway Account.
- The payer is sent to `secure.wayforpay.com` by a signed form (`/wayforpay-checkout?order=...`). The amount and the
  currency stay on the server in an Integration Request; the address carries only its name.
- WayForPay reports the result to `/api/method/wayforpay.api.notify`. The notification is believed only when its
  signature (HMAC-MD5 with the secret key) matches and its amount and currency are those of the order; a repeated
  notification pays nothing twice. The payer returns to `/wayforpay-result`, which waits for the notification and goes
  to the success or the failure page.
- Currencies: UAH, USD, EUR.

Test merchant of WayForPay (public, from their documentation): account `test_merch_n1`, domain `www.market.ua`.

Developed in the Docker dev bench of `tasmail/erpnext-infra`; the unit tests need a Frappe site
(`bench --site dev.localhost run-tests --app wayforpay`), CI checks the style (`ruff`).
