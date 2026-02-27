import stripe

from django.conf import settings
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.db import transaction
from .models import EnrolmentPayment


stripe.api_key = settings.STRIPE_SECRET_KEY

@csrf_exempt
@require_POST
def create_checkout_session(request, payment_id: int):
    if not settings.STRIPE_SECRET_KEY:
        return HttpResponseBadRequest("Stripe secret key missing (STRIPE_SECRET_KEY).")

    try:
        payment = EnrolmentPayment.objects.select_related("enrolment", "enrolment__plan").get(pk=payment_id)
    except EnrolmentPayment.DoesNotExist:
        return HttpResponseBadRequest("Payment not found.")

    # Don’t recreate if already has a link/session
    if payment.checkout_url and payment.stripe_checkout_session_id:
        return JsonResponse({
            "ok": True,
            "payment_id": payment.payment_id,
            "checkout_url": payment.checkout_url,
            "already_created": True,
        })

    if payment.status not in ("requested", "pending"):
        return HttpResponseBadRequest(f"Payment status must be requested/pending, got {payment.status!r}")

    if not payment.amount_pence or payment.amount_pence <= 0:
        return HttpResponseBadRequest("Payment amount_pence is missing/invalid.")

    enrolment = payment.enrolment
    if enrolment is None:
        return HttpResponseBadRequest("Payment has no enrolment attached.")

    purpose_label = {
        "membership_fee": "Membership fee",
        "share_capital": "Share capital investment",
        "other": "Payment",
    }.get(payment.purpose, "Payment")

    enrolment_code = enrolment.enrolment_code or f"enrolment-{enrolment.enrolment_id}"

    success_url = getattr(settings, "STRIPE_SUCCESS_URL", "") + f"?payment_id={payment.payment_id}"
    cancel_url = getattr(settings, "STRIPE_CANCEL_URL", "") + f"?payment_id={payment.payment_id}"

    session = stripe.checkout.Session.create(
        mode="payment",
        success_url=success_url,
        cancel_url=cancel_url,
        line_items=[{
            "price_data": {
                "currency": (payment.currency or "GBP").lower(),
                "unit_amount": int(payment.amount_pence),
                "product_data": {
                    "name": f"{purpose_label} — {enrolment_code}",
                },
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

    return JsonResponse({
        "ok": True,
        "payment_id": payment.payment_id,
        "checkout_url": payment.checkout_url,
        "stripe_checkout_session_id": payment.stripe_checkout_session_id,
    })


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """
    Stripe webhook: mark payment succeeded and activate enrolment if fully paid.
    """
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    if not settings.STRIPE_WEBHOOK_SECRET:
        return HttpResponseBadRequest("STRIPE_WEBHOOK_SECRET missing.")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=settings.STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError: # type: ignore
        return HttpResponse(status=400)

    event_type = event.get("type")

    # Handle Checkout success for both sync + async payment methods
    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        session = event["data"]["object"]
        session_id = session.get("id")
        payment_status = session.get("payment_status")  # 'paid', 'unpaid', etc.

        # Only mark as paid when Stripe confirms it
        if payment_status != "paid":
            return HttpResponse(status=200)

        metadata = session.get("metadata") or {}
        payment_id_str = metadata.get("payment_id")

        try:
            payment_id = int(payment_id_str) if payment_id_str else None
        except (TypeError, ValueError):
            payment_id = None

        now = timezone.now()

        with transaction.atomic():
            payment = None

            if payment_id is not None:
                payment = (
                    EnrolmentPayment.objects
                    .select_for_update()
                    .filter(pk=payment_id)
                    .first()
                )

            if not payment:
                payment = (
                    EnrolmentPayment.objects
                    .select_for_update()
                    .filter(stripe_checkout_session_id=session_id)
                    .first()
                )

            if not payment:
                # Acknowledge anyway so Stripe doesn't retry forever
                return HttpResponse(status=200)

            # Idempotency: webhook may be delivered multiple times
            if payment.status != "succeeded":
                payment.status = "succeeded"
                payment.paid_at = now
                payment.provider = "stripe"
                payment.provider_ref = session.get("payment_intent") or payment.provider_ref

                # Consider storing a trimmed payload instead of the whole session
                payment.provider_payload = {
                    "event_id": event.get("id"),
                    "event_type": event_type,
                    "checkout_session_id": session_id,
                    "payment_intent": session.get("payment_intent"),
                    "payment_status": payment_status,
                    "amount_total": session.get("amount_total"),
                    "currency": session.get("currency"),
                    "customer": session.get("customer"),
                }

                payment.save(update_fields=[
                    "status", "paid_at", "provider", "provider_ref", "provider_payload"
                ])

            enrolment = payment.enrolment
            if enrolment and enrolment.status != "active" and enrolment.is_fully_paid():
                enrolment.status = "active"
                enrolment.date_start = enrolment.date_start or now.date()
                enrolment.save(update_fields=["status", "date_start"])

        return HttpResponse(status=200)

    return HttpResponse(status=200)