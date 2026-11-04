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
  `Requested`, `Initiated` or `Partially Paid`, an Inward request that is not a Phone one, the same currency, and an
  amount equal to the paid one within 0.005; the newest wins. `Initiated` is included because ERPNext sets it on every
  request that has a payment url (every request made for a gateway account); `Partially Paid` because a partly paid
  request is payable for what is outstanding. `Pending` is not a status of a Payment Request. An Outward request (a
  payment to a supplier, also `Initiated`) and a Phone one (its payment books nothing) are never chosen.
- the amount compared is the one that booking pays, the `outstanding_amount` of the live request (its `grand_total` when
  that is empty): a partly paid request is matched by what is still owed.
- the chosen request is read again right before booking; when it was paid meanwhile (an `Initiated` request can be
  paid on its own page) nothing is booked and the payment is marked `unbooked`, so the money is never booked twice.
- the booking runs in a database savepoint. When it raises, it is rolled back to the savepoint, so no half-made Payment
  Entry stays. Only rows of the database are undone: the callbacks that run after the commit (jobs queued with
  `enqueue_after_commit`, such as the payment e-mail), documents held in memory, `frappe.flags` and Redis writes are not
  reverted, and a commit inside the booking destroys the savepoint, so the rollback then fails. The traceback is taken
  in the `except` block and the Error Log is written after the rollback (before it, the row would be undone with it).
  When the rollback fails, the note says that a Payment Entry may exist and asks to check the existing ones first.
- the booking counts as made only when the request, read again afterwards, left the open states or owes less than before:
  `on_payment_authorized` of Webshop returns without booking for a disabled cart or a hook, and that must not look like
  a booked payment. When the reading itself fails, the booking is unknown: it is rolled back if possible, nothing is
  linked and the note asks to check the existing Payment Entries first. Otherwise the payment is marked `unbooked` ("returned without a Payment Entry") and nothing is linked.
  The payment of a request that is not cancelled goes the old way, untouched.
- the Integration Request is linked to the booked request (`reference_docname`) only after the booking has succeeded, and
  its output keeps the name of the cancelled one (`cancelled_request`) for the audit. A failure of this bookkeeping is
  logged ("a booked payment was not linked to its request") and is never reported as a payment that is not booked.
- when no request fits, or the booking raises, the Integration Request stays `Completed` (the money is received) and its
  output carries `unbooked`; an Error Log "WayForPay: payment received for a cancelled request, book manually" and a
  comment on the document of the request tell a manager to book the payment by hand. The hourly reconcile skips it.
  After a booking that raised, the note says whether it was rolled back; check the existing Payment Entries of the
  document before booking by hand in any case. A failure of the note itself is logged and never hides the original one.
- a notification that was lost for such a payment is recovered too: when ERPNext cancels the open Draft or Requested
  requests of a document (`cancel_old_payment_requests`) it sets their `Queued` Integration Requests to `Cancelled`; the
  hourly reconcile asks WayForPay about these too (as about `Queued` ones, for three days) and books the answer through
  the same logic. A booked payment is `Completed` and a repeated answer changes nothing; a payment that was never made
  is answered "declined" or unknown and is dropped after the three days.

The choice is `wayforpay.api.pick_live`; its tests and those of the booking are mocked Python
(`wayforpay/tests/test_cancelled.py`, no site needed and no global state touched); the flow through the notification and
the reconcile is tested in `test_flow.py` (needs a site).
