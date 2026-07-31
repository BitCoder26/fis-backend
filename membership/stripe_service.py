from urllib.parse import urlencode

import stripe
from django.conf import settings
from django.utils import timezone

from .models import Payment


def ensure_checkout_session(payment: Payment) -> Payment:
    """
    Creates a Stripe Checkout Session if the payment doesn't already have one.
    Updates payment fields: stripe_checkout_session_id, checkout_url, status, etc.
    Returns the updated payment.
    """
    if not settings.STRIPE_SECRET_KEY:
        raise ValueError("Stripe secret key missing (STRIPE_SECRET_KEY).")

    stripe.api_key = settings.STRIPE_SECRET_KEY

    # Refresh from DB so we don't work with stale state
    payment = Payment.objects.select_related("enrolment", "enrolment__plan").get(pk=payment.pk)

    # Don't recreate if already has a link/session
    if payment.checkout_url and payment.stripe_checkout_session_id:
        return payment

    if payment.status not in ("requested", "pending"):
        raise ValueError(f"Payment status must be requested/pending, got {payment.status!r}")

    if not payment.amount_pence or payment.amount_pence <= 0:
        raise ValueError("Payment amount_pence is missing/invalid.")

    enrolment = payment.enrolment
    if enrolment is None:
        raise ValueError("Payment has no enrolment attached.")

    purpose_label = {
        "membership_fee": "Membership fee",
        "share_capital": "Share capital investment",
        "other": "Payment",
    }.get(payment.purpose, "Payment")

    enrolment_code = enrolment.enrolment_code or f"enrolment-{enrolment.enrolment_id}"

    success_extra = urlencode({
        "amount": f"{(payment.amount_pence or 0) / 100:.2f}",
        "currency": (payment.currency or "GBP").upper(),
        "ref": enrolment_code,
    })
    success_url = getattr(settings, "STRIPE_SUCCESS_URL", "") + f"?payment_id={payment.payment_id}&{success_extra}"
    cancel_url = getattr(settings, "STRIPE_CANCEL_URL", "") + f"?payment_id={payment.payment_id}"

    session = stripe.checkout.Session.create(
        mode="payment",
        success_url=success_url,
        cancel_url=cancel_url,
        line_items=[{
            "price_data": {
                "currency": (payment.currency or "GBP").lower(),
                "unit_amount": int(payment.amount_pence),
                "product_data": {"name": f"{purpose_label} — {enrolment_code}"},
            },
            "quantity": 1,
        }],
        metadata={
            "payment_id": str(payment.payment_id),
            "enrolment_id": str(enrolment.enrolment_id),
            "purpose": str(payment.purpose),
        },
    )

    payment.stripe_checkout_session_id = session.id
    payment.checkout_url = session.url
    payment.provider = "stripe"
    payment.stripe_payment_intent_id = session.get("payment_intent")
    payment.status = "pending"
    payment.requested_at = payment.requested_at or timezone.now()
    payment.provider_payload = {"checkout_session_id": session.id}

    payment.save(update_fields=[
        "stripe_checkout_session_id",
        "checkout_url",
        "provider",
        "stripe_payment_intent_id",
        "status",
        "requested_at",
        "provider_payload",
    ])

    return payment
