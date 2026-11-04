# wayforpay

[WayForPay](https://wayforpay.com) payment gateway for the Frappe `payments` app: a Payment Request (ERPNext,
Webshop) can be paid through it like through Stripe.

- **WayForPay Settings** (one per merchant): merchant account, merchant domain (as registered in WayForPay) and the
  secret key. Saving it creates the Payment Gateway `WayForPay-<name>` and its Payment Gateway Account.
- The payer is sent to `secure.wayforpay.com` by a signed form (`/wayforpay_checkout?order=...`). The amount and the
  currency stay on the server in an Integration Request; the address carries only its name.
- WayForPay reports the result to `/api/method/wayforpay.api.notify`. The notification is believed only when its
  signature (HMAC-MD5 with the secret key) matches and its amount and currency are those of the order; a repeated
  notification pays nothing twice. The payer returns to `/wayforpay_result`, which waits for the notification and goes
  to the success or the failure page.
- The payer comes back by a cross-site POST to `/api/method/wayforpay.api.returned` (returnUrl), which redirects to
  `/wayforpay_result`. The browser does not send the `sid` cookie (SameSite=Lax) with it, so Frappe sees a Guest and
  would overwrite the cookies of a logged in payer with Guest ones; `returned` therefore clears the cookies of the
  response and sets none, and the payer stays logged in. Do not change the cookies to SameSite=None for this.
- Currencies: UAH, USD, EUR.
- Without the `payments` app (a site that is not a shop) the gateway still works: the code that starts a payment
  names a function in the data of the order (`on_paid`, a dotted path), and the notification calls it with the order
  and the reference of the payment. `wayforpay.rates.rate("USD")` is the official rate of the National Bank of
  Ukraine (cached for an hour) for a payment that is priced in another currency.

Test merchant of WayForPay (public, from their documentation): account `test_merch_n1`, domain `www.market.ua`.

Developed in the Docker dev bench of `tasmail/erpnext-infra`; the unit tests need a Frappe site
(`bench --site dev.localhost run-tests --app wayforpay`), CI checks the style (`ruff`).

## A payment for a cancelled Payment Request

A shop can cancel the open Payment Request of an order when the customer asks to pay again, while the customer still
pays on the first page of WayForPay. The notification of that payment names the cancelled request, which Frappe
refuses to update. The app does not lose the money and does not cancel or create anything:

- the payment is booked (`on_payment_authorized`) on the live Payment Request of the same document: docstatus 1, status
  `Requested` or `Pending`, the same currency and a `grand_total` equal to the paid amount (tolerance 0.005); the
  newest one wins. The Integration Request is linked to the request that was booked (`reference_docname`);
- when no request fits, the Integration Request stays `Completed` (the money is received) and its output carries
  `unbooked`; an Error Log "WayForPay: payment received for a cancelled request, book manually" and a comment on the
  document of the request tell a manager to book the payment by hand. The hourly reconcile skips such a payment.

The choice is `wayforpay.api.pick_live`; its tests are pure Python (`wayforpay/tests/test_cancelled.py`), the booking
is tested in `test_flow.py` (needs a site).
