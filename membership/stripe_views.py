from urllib.parse import urlencode

import requests
import stripe

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.db import transaction
from django.db.models import Q

from .models import Payment, find_enrolment_by_email
from .emails import send_donation_receipt_email, send_membership_payment_received_email


stripe.api_key = settings.STRIPE_SECRET_KEY


def _payment_reference(payment: Payment) -> str:
    """A human-friendly reference to show the payer on the thank-you page."""
    if payment.enrolment_id and payment.enrolment and payment.enrolment.enrolment_code:
        return payment.enrolment.enrolment_code
    return f"DON-{payment.payment_id:06d}"


def _build_success_url(payment: Payment, source: str) -> str:
    base = settings.STRIPE_SUCCESS_URL.format(payment_id=payment.payment_id)
    extra = urlencode({
        "source": source,
        "amount": f"{(payment.amount_pence or 0) / 100:.2f}",
        "currency": payment.currency or "GBP",
        "ref": _payment_reference(payment),
    })
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{extra}"


def _build_cancel_url(payment: Payment, source: str) -> str:
    base = settings.STRIPE_CANCEL_URL.format(payment_id=payment.payment_id)
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}source={source}"


def _build_item_name(payment: Payment) -> str:
    if payment.purpose == "donation":
        if payment.enrolment and payment.enrolment.enrolment_code:
            return f"FIS donation - {payment.enrolment.enrolment_code}"
        return "FIS donation"

    purpose_label = {
        "membership_fee": "Membership fee",
        "annual_fee": "Annual fee",
        "share_capital": "Share capital investment",
        "other": "Payment",
    }.get(payment.purpose, "Payment")

    enrolment = payment.enrolment
    enrolment_code = None
    if enrolment is not None:
        enrolment_code = enrolment.enrolment_code or f"enrolment-{enrolment.enrolment_id}"

    if enrolment_code:
        return f"{purpose_label} - {enrolment_code}"
    return f"FIS {purpose_label.lower()}"


def _create_stripe_session(payment: Payment, source: str) -> Payment:
    if not settings.STRIPE_SECRET_KEY:
        raise ValueError("Stripe secret key missing (STRIPE_SECRET_KEY).")

    if payment.checkout_url and payment.stripe_checkout_session_id:
        return payment

    if payment.status not in ("requested", "pending"):
        raise ValueError(f"Payment status must be requested/pending, got {payment.status!r}")

    if not payment.amount_pence or payment.amount_pence <= 0:
        raise ValueError("Payment amount_pence is missing/invalid.")

    enrolment_id = str(payment.enrolment.enrolment_id) if payment.enrolment_id else ""
    enrolment_code = payment.enrolment.enrolment_code if payment.enrolment_id else ""

    session = stripe.checkout.Session.create(
        mode="payment",
        success_url=_build_success_url(payment, source),
        cancel_url=_build_cancel_url(payment, source),
        customer_email=payment.donor_email or None,
        line_items=[{
            "price_data": {
                "currency": (payment.currency or "GBP").lower(),
                "unit_amount": int(payment.amount_pence),
                "product_data": {"name": _build_item_name(payment)},
            },
            "quantity": 1,
        }],
        metadata={
            "payment_id": str(payment.payment_id),
            "purpose": str(payment.purpose),
            "enrolment_id": enrolment_id,
            "enrolment_code": enrolment_code,
            "donor_name": payment.donor_name or "",
            "donor_email": payment.donor_email or "",
            "source": source,
        },
        # Stamp our id onto the PaymentIntent/Charge too, so later events that
        # don't carry the checkout-session metadata (refunds, disputes) can be
        # matched back to this payment.
        payment_intent_data={
            "metadata": {
                "payment_id": str(payment.payment_id),
                "purpose": str(payment.purpose),
            },
        },
    )

    payment.stripe_checkout_session_id = session.id
    payment.checkout_url = session.url
    payment.provider = "stripe"
    payment.stripe_payment_intent_id = session.get("payment_intent")
    payment.status = "pending"
    payment.requested_at = payment.requested_at or timezone.now()
    payment.provider_payload = {
        "checkout_session_id": session.id,
        "source": source,
        "matched_enrolment_code": enrolment_code or None,
    }
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


@csrf_exempt
@require_POST
def create_checkout_session(request, payment_id: int):
    try:
        payment = Payment.objects.select_related("enrolment", "enrolment__plan").get(pk=payment_id)
    except Payment.DoesNotExist:
        return HttpResponseBadRequest("Payment not found.")

    try:
        payment = _create_stripe_session(payment, source="membership")
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    return JsonResponse({
        "ok": True,
        "payment_id": payment.payment_id,
        "checkout_url": payment.checkout_url,
        "stripe_checkout_session_id": payment.stripe_checkout_session_id,
    })


def _verify_turnstile(request):
    """Verify a Cloudflare Turnstile token. Returns (ok, error_message)."""
    token = (
        request.POST.get("turnstile_token")
        or request.POST.get("cf-turnstile-response")
        or request.POST.get("cf_turnstile_response")
    )
    if not token:
        return False, "Turnstile token missing."
    if not settings.TURNSTILE_SECRET_KEY:
        return False, "Turnstile is not configured."

    try:
        resp = requests.post(
            settings.TURNSTILE_VERIFY_URL,
            data={
                "secret": settings.TURNSTILE_SECRET_KEY,
                "response": token,
                "remoteip": request.META.get("REMOTE_ADDR"),
            },
            timeout=5,
        )
        result = resp.json()
    except requests.RequestException:
        return False, "Turnstile verification request failed."
    except ValueError:
        return False, "Turnstile verification returned invalid JSON."

    if not result.get("success"):
        return False, "Turnstile verification failed."
    return True, None


@csrf_exempt
@require_POST
def create_donation_checkout_session(request):
    ok, turnstile_error = _verify_turnstile(request)
    if not ok:
        return HttpResponseBadRequest(turnstile_error)

    donor_name = (request.POST.get("full-name") or "").strip()
    donor_email = (request.POST.get("email") or "").strip().lower()
    donation_amount = (request.POST.get("donation-amount") or "").strip()

    if donation_amount == "other":
        raw_amount = request.POST.get("custom-donation-amount")
    else:
        raw_amount = donation_amount

    if not donor_name:
        return HttpResponseBadRequest("Full name is required.")
    if not donor_email:
        return HttpResponseBadRequest("Email address is required.")

    try:
        amount_pence = int(round(float(raw_amount) * 100))
    except (TypeError, ValueError):
        return HttpResponseBadRequest("Donation amount must be a valid number.")

    if amount_pence <= 0:
        return HttpResponseBadRequest("Donation amount must be greater than 0.")

    matched_enrolment = find_enrolment_by_email(donor_email)

    with transaction.atomic():
        payment = Payment.objects.create(
            enrolment=matched_enrolment,
            donor_name=donor_name,
            donor_email=donor_email,
            purpose="donation",
            status="requested",
            amount_pence=amount_pence,
            currency="GBP",
            payment_method="online_payment",
            requested_at=timezone.now(),
            provider="stripe",
        )

        try:
            payment = _create_stripe_session(payment, source="donation")
        except ValueError as exc:
            transaction.set_rollback(True)
            return HttpResponseBadRequest(str(exc))
        except stripe.StripeError as exc:
            transaction.set_rollback(True)
            message = getattr(exc, "user_message", None) or str(exc)
            return HttpResponseBadRequest("Payment provider error: %s" % message)

    return redirect(payment.checkout_url)


def _find_payment_for_session(session):
    """Match a Stripe Checkout Session back to our Payment row (locked)."""
    metadata = session.get("metadata") or {}
    payment_id_str = metadata.get("payment_id")
    try:
        payment_id = int(payment_id_str) if payment_id_str else None
    except (TypeError, ValueError):
        payment_id = None

    payment = None
    if payment_id is not None:
        payment = Payment.objects.select_for_update().filter(pk=payment_id).first()
    if not payment:
        payment = Payment.objects.select_for_update().filter(
            stripe_checkout_session_id=session.get("id")
        ).first()
    return payment


def _find_payment_for_charge(charge):
    """Match a Stripe Charge (e.g. from a refund) back to our Payment row (locked)."""
    payment_intent = charge.get("payment_intent")
    payment = None
    if payment_intent:
        payment = Payment.objects.select_for_update().filter(
            Q(stripe_payment_intent_id=payment_intent) | Q(provider_ref=payment_intent)
        ).first()
    if not payment:
        # Fallback: the id we stamped onto the PaymentIntent metadata.
        meta = charge.get("metadata") or {}
        pid = meta.get("payment_id")
        if pid:
            payment = Payment.objects.select_for_update().filter(pk=pid).first()
    return payment


def _handle_checkout_paid(event, session):
    if session.get("payment_status") != "paid":
        return
    with transaction.atomic():
        payment = _find_payment_for_session(session)
        if not payment:
            return

        payment_intent = session.get("payment_intent")
        newly_succeeded = payment.status != "succeeded"
        if newly_succeeded:
            payment.status = "succeeded"
            payment.paid_at = timezone.now()
            payment.provider = "stripe"
            payment.provider_ref = payment_intent or payment.provider_ref
            payment.stripe_payment_intent_id = payment_intent or payment.stripe_payment_intent_id
            payment.provider_payload = {
                "event_id": event.get("id"),
                "event_type": event.get("type"),
                "checkout_session_id": session.get("id"),
                "payment_intent": payment_intent,
                "payment_status": session.get("payment_status"),
                "amount_total": session.get("amount_total"),
                "currency": session.get("currency"),
                "customer": session.get("customer"),
                "donor_email": payment.donor_email,
                "matched_enrolment_code": payment.enrolment.enrolment_code if payment.enrolment_id else None,
            }
            payment.save(update_fields=[
                "status",
                "paid_at",
                "provider",
                "provider_ref",
                "stripe_payment_intent_id",
                "provider_payload",
            ])

        enrolment = payment.enrolment
        if enrolment and enrolment.status != "active" and enrolment.is_fully_paid():
            enrolment.status = "active"
            enrolment.date_start = enrolment.date_start or timezone.now().date()
            enrolment.save(update_fields=["status", "date_start"])

        # Email the donor a receipt — only for donations, and only on the first
        # transition to "succeeded" (webhooks can fire more than once, so this
        # guards against duplicate receipts). Deferred to on_commit so a slow or
        # failing send can never block or roll back the webhook transaction.
        if newly_succeeded and payment.purpose == "donation" and payment.donor_email:
            receipt_kwargs = {
                "to_email": payment.donor_email,
                "donor_name": payment.donor_name,
                "amount_pence": payment.amount_pence,
                "reference": _payment_reference(payment),
                "currency": payment.currency,
                "donated_on": timezone.now().strftime("%d %B %Y"),
            }

            def _send_receipt():
                try:
                    send_donation_receipt_email(**receipt_kwargs)
                except Exception as exc:  # never break the webhook on email failure
                    print("Donation receipt email failed:", exc)

            transaction.on_commit(_send_receipt)
        elif enrolment and enrolment.status == "active" and not enrolment.membership_activated_email_sent_at:
            to_email = enrolment.get_contact_email()
            if to_email:
                marketing_consent = enrolment.declarations.filter(
                    is_current=True
                ).values_list("data_marketing_consent", flat=True).first()
                receipt_kwargs = {
                    "to_email": to_email,
                    "enrolment_code": enrolment.enrolment_code or str(enrolment.enrolment_id),
                    "amount_pence": payment.amount_pence,
                    "currency": payment.currency,
                    "paid_on": (payment.paid_at or timezone.now()).strftime("%d %B %Y"),
                    "plan_name": enrolment.plan.name if enrolment.plan_id else None,
                    "marketing_consent": bool(marketing_consent),
                }

                def _send_membership_receipt():
                    try:
                        send_membership_payment_received_email(**receipt_kwargs)
                    except Exception as exc:  # leave the marker unset for a manual event replay
                        print("Membership payment receipt email failed:", exc)
                        return

                    enrolment.membership_activated_email_sent_at = timezone.now()
                    enrolment.save(update_fields=["membership_activated_email_sent_at"])

                transaction.on_commit(_send_membership_receipt)


def _handle_checkout_not_completed(session, new_status):
    """Mark an abandoned/expired or async-failed checkout. Never overrides a
    payment that already succeeded or was refunded."""
    with transaction.atomic():
        payment = _find_payment_for_session(session)
        if not payment:
            return
        if payment.status in ("requested", "pending"):
            payment.status = new_status
            payment.save(update_fields=["status"])


def _handle_charge_refunded(event, charge):
    with transaction.atomic():
        payment = _find_payment_for_charge(charge)
        if not payment:
            return

        amount = charge.get("amount") or 0
        amount_refunded = charge.get("amount_refunded") or 0
        fully_refunded = bool(charge.get("refunded")) or (amount and amount_refunded >= amount)
        new_status = "refunded" if fully_refunded else "part_refunded"

        if payment.status == new_status:
            return  # idempotent — already recorded

        payment.status = new_status
        payload = payment.provider_payload if isinstance(payment.provider_payload, dict) else {}
        payload.update({
            "refund_event_id": event.get("id"),
            "refund_event_type": event.get("type"),
            "charge_id": charge.get("id"),
            "payment_intent": charge.get("payment_intent"),
            "amount_refunded": amount_refunded,
            "amount_charged": amount,
        })
        payment.provider_payload = payload
        payment.save(update_fields=["status", "provider_payload"])


@csrf_exempt
@require_POST
def stripe_webhook(request):
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
    except stripe.SignatureVerificationError:
        return HttpResponse(status=400)

    event_type = event.get("type")
    obj = event["data"]["object"]

    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        _handle_checkout_paid(event, obj)
    elif event_type == "checkout.session.expired":
        _handle_checkout_not_completed(obj, new_status="cancelled")
    elif event_type == "checkout.session.async_payment_failed":
        _handle_checkout_not_completed(obj, new_status="failed")
    elif event_type in ("charge.refunded", "charge.refund.updated"):
        _handle_charge_refunded(event, obj)

    # Any other event type: acknowledge so Stripe stops retrying.
    return HttpResponse(status=200)
