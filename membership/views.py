from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.shortcuts import redirect 
from urllib.parse import urlencode 
from .emails import send_application_submitted_email, submit_newsletter_opt_in
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from .models import Application
from .emails import send_membership_activated_email
from .models import Payment
from django.db import transaction
from django.contrib.auth import get_user_model
from .admin import _to_bool
from .enrolment_workflow import process_application, approve_enrolment, request_payment
from .stripe_service import ensure_checkout_session
import requests
import stripe


stripe.api_key = settings.STRIPE_SECRET_KEY

# Membership plans (m1 = individual annual, m2 = founder/individual lifetime):
# when there is no conflict of interest, skip the "application received" and
# "payment link" emails entirely and send the applicant straight to Stripe
# checkout. They should get a single email once payment succeeds.
DIRECT_CHECKOUT_PLANS = {"CM-IA", "CM-IL"}

PLAN_TO_FORM = {
    # Community members (CM)
    "CM-IA": "individual-annual",
    "CM-IL": "founder-individual-lifetime",
    "CM-CS": "community-share-owner",
    "CM-PS": "pioneer-share-owner",

    # Non-members (NM)
    "NM-VO": "volunteer",
    "NM-SU": "supporter",
    # "NM-AF": "affiliate-partner",
    # "NM-ST": "strategic-partner",
    "NM-CN": "community-food-non-profit-partner",
    "NM-CF": "community-food-for-profit-partner",
}


def _submit_newsletter_opt_in(*, email: str, consent_marketing) -> bool:
    if not email or _to_bool(consent_marketing) is not True:
        return False

    try:
        submit_newsletter_opt_in(email=email)
    except Exception as e:
        # Newsletter signup is best-effort and must never fail enrolment.
        print("Brevo newsletter opt-in failed:", e)
        return False

    return True


@csrf_exempt
def submit_enrolment(request):
    print(">>> submit_enrolment CALLED")
    if request.method == "GET":
        return HttpResponse("OK - use POST from the form.", content_type="text/plain")

    if request.method != "POST":
        return HttpResponse("Method not allowed", status=405)
    
    token = (
        request.POST.get("turnstile_token")
        or request.POST.get("cf-turnstile-response")
        or request.POST.get("cf_turnstile_response")
    )
    plan = request.POST.get("plan", "").strip()

    if not token:
        return HttpResponseBadRequest("Turnstile token missing.")
    if not plan:
        return HttpResponseBadRequest("plan missing.")

    # Verify Turnstile
    try:
        verify_response = requests.post(
            settings.TURNSTILE_VERIFY_URL,
            data={
                "secret": settings.TURNSTILE_SECRET_KEY,
                "response": token,
                "remoteip": request.META.get("REMOTE_ADDR"),
            },
            timeout=5,
        )
        result = verify_response.json()
    except requests.RequestException:
        return HttpResponseBadRequest("Turnstile verification request failed.")
    except ValueError:
        return HttpResponseBadRequest("Turnstile verification returned invalid JSON.")

    if not result.get("success"):
        print("Turnstile verification failed:", result)
        return HttpResponseBadRequest("Turnstile verification failed.")

    # Save submission
    raw = dict(request.POST)
    raw.pop("turnstile_token", None)
    raw.pop("plan", None)
    raw.pop("cf_turnstile_response", None)

    # Convert kebab-case names (e.g. first-name) to snake_case (e.g. first_name)
    flat = {k: (v[0] if isinstance(v, list) else v) for k, v in raw.items()}
    flat = {k.replace("-", "_"): v for k, v in flat.items()}
    logo = request.FILES.get("logo_file")
    to_email = (flat.get("email") or "").strip()

    app = Application.objects.create(
        plan=plan,
        data=flat,
        logo_file=logo,
        ip_address=request.META.get("REMOTE_ADDR"),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        turnstile_success=True,
        turnstile_error_codes=result.get("error-codes", []),
    )

    _submit_newsletter_opt_in(
        email=to_email,
        consent_marketing=flat.get("consent_marketing"),
    )

    coi = (_to_bool(flat.get("conflict_of_interest")) is True)
    direct_checkout = (plan.upper() in DIRECT_CHECKOUT_PLANS) and not coi

    # Send the "application received" email FIRST — before the auto-approval
    # workflow can send the activation / payment-link email — so the applicant
    # sees the acknowledgement as the first message, then the outcome.
    # m1/m2 applications with no conflict of interest skip straight to Stripe
    # checkout instead, so they get no email until payment succeeds.
    if to_email and not app.application_submitted_email_sent_at and not direct_checkout:
        try:
            send_application_submitted_email(
                to_email=to_email,
                plan_name=plan,                          # or map to Plan.name later if you want
                plan_code=plan,
            )
            app.application_submitted_email_sent_at = timezone.now()
            app.save(update_fields=["application_submitted_email_sent_at"])
        except Exception as e:
            # Don't break submission if email fails
            print("Application received email failed:", e)

    checkout_payment_id = None

    try:
        with transaction.atomic():
            if not coi:
                # pick a system user for approved_by (best practice: create a dedicated one)
                User = get_user_model()
                system_user = (
                    User.objects.filter(username="fis-system").first()
                    or User.objects.filter(is_superuser=True).order_by("id").first()
                )

                if not system_user:
                    print("fis-system user missing; skipping auto approve/payment")

                else:
                    enrolment = process_application(application_id=app.pk)
                    if enrolment is not None:
                        enrolment = approve_enrolment(enrolment=enrolment, approved_by=system_user)
                        payment = request_payment(enrolment=enrolment, send_email=not direct_checkout)
                        if direct_checkout and payment is not None:
                            checkout_payment_id = payment.pk
    except Exception as e:
        print("Auto workflow failed:", e)
        checkout_payment_id = None

    if direct_checkout and checkout_payment_id:
        try:
            payment = ensure_checkout_session(Payment.objects.get(pk=checkout_payment_id))
            if payment.checkout_url:
                return redirect(payment.checkout_url)
        except Exception as e:
            print("Immediate checkout session creation failed:", e)

    # Fallback for the direct-checkout path if anything above didn't produce a
    # checkout redirect (e.g. system user missing, payment creation failed) —
    # make sure the applicant still gets the acknowledgement email.
    if direct_checkout and to_email and not app.application_submitted_email_sent_at:
        try:
            send_application_submitted_email(
                to_email=to_email,
                plan_name=plan,
                plan_code=plan,
            )
            app.application_submitted_email_sent_at = timezone.now()
            app.save(update_fields=["application_submitted_email_sent_at"])
        except Exception as e:
            print("Application received email failed (fallback):", e)

    form_slug = PLAN_TO_FORM.get(plan, "submission")
    qs = urlencode({"form": form_slug})
    return redirect(f"{settings.THANKS_BASE}?{qs}")

@csrf_exempt
@require_POST
def create_checkout(request, payment_id: int):
    try:
        p = Payment.objects.select_related("enrolment").get(pk=payment_id)
    except Payment.DoesNotExist:
        return JsonResponse({"ok": False, "error": "payment not found"}, status=404)

    if not p.amount_pence or p.amount_pence <= 0:
        return JsonResponse({"ok": False, "error": "amount_pence must be > 0"}, status=400)

    # If already created, return existing URL (idempotent)
    if p.checkout_url and p.stripe_checkout_session_id and p.status in ("requested", "pending"):
        return JsonResponse({"ok": True, "checkout_url": p.checkout_url, "session_id": p.stripe_checkout_session_id})

    success_url = settings.STRIPE_SUCCESS_URL.format(payment_id=p.payment_id)
    cancel_url  = settings.STRIPE_CANCEL_URL.format(payment_id=p.payment_id)

    code = (p.enrolment.enrolment_code if p.enrolment else None) or f"enrolment-{p.enrolment_id or 'unknown'}"
    item_name = f"FIS payment ({p.purpose}) — {code}"

    session = stripe.checkout.Session.create(
        mode="payment",
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={
            "payment_id": str(p.payment_id),  # legacy
            "enrolment_payment_id": str(p.payment_id),
            "enrolment_id": str(p.enrolment_id or ""),
            "purpose": p.purpose,
        },
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": (p.currency or "GBP").lower(),
                "unit_amount": int(p.amount_pence),
                "product_data": {"name": item_name},
            },
        }],
    )

    p.status = "pending"
    p.provider = "stripe"
    p.stripe_checkout_session_id = session.id
    p.checkout_url = session.url
    p.save(update_fields=["status", "provider", "stripe_checkout_session_id", "checkout_url"])

    return JsonResponse({"ok": True, "checkout_url": session.url, "session_id": session.id})


@csrf_exempt
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=settings.STRIPE_WEBHOOK_SECRET,  # whsec_...
        )
    except (ValueError, stripe.SignatureVerificationError):
        return HttpResponse(status=400)

    event_type = event.get("type")

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        print("SESSION METADATA:", session.get("metadata"))

        # Only act on successful payments
        if session.get("payment_status") != "paid":
            return HttpResponse(status=200)

        md = session.get("metadata") or {}
        payment_id = md.get("enrolment_payment_id") or md.get("payment_id")

        # If it's not one of our sessions, ignore
        if not payment_id:
            return HttpResponse(status=200)
        
        _handle_checkout_completed(payment_id, session)
        return HttpResponse(status=200)

    return HttpResponse(status=200)

@transaction.atomic
def _handle_checkout_completed(payment_id: str, session_obj: dict):
    p = (
        Payment.objects
        .select_for_update()
        .get(pk=int(payment_id))
    )

    changed = False

    # Mark succeeded
    if p.status != "succeeded":
        p.status = "succeeded"
        changed = True

    # Set paid_at once
    if not p.paid_at:
        p.paid_at = timezone.now()
        changed = True

    # Always keep Stripe data synced
    if p.stripe_payment_intent_id != session_obj.get("payment_intent"):
        p.stripe_payment_intent_id = session_obj.get("payment_intent")
        changed = True

    if p.stripe_checkout_session_id != session_obj.get("id"):
        p.stripe_checkout_session_id = session_obj.get("id")
        changed = True

    p.provider_payload = session_obj  # optional but useful
    changed = True

    if changed:
        p.save(update_fields=[
            "status",
            "paid_at",
            "provider_payload",
            "stripe_payment_intent_id",
            "stripe_checkout_session_id",
        ])

    e = p.enrolment
    if not e:
        return

    # Activate enrolment if fully paid
    if e.status != "active" and e.is_fully_paid():
        e.status = "active"
        e.date_start = e.date_start or timezone.now().date()
        e.save(update_fields=["status", "date_start"])

    # Send activation email once
    if e.status == "active" and not e.membership_activated_email_sent_at:
        to_email = e.get_contact_email()
        if to_email:
            send_membership_activated_email(
                to_email=to_email,
                enrolment_code=e.enrolment_code or str(e.enrolment_id),
                plan_name=e.plan.name if e.plan_id else None,
            )
            e.membership_activated_email_sent_at = timezone.now()
            e.save(update_fields=["membership_activated_email_sent_at"])

from django.http import HttpResponse
from django.conf import settings

def version_view(request):
    return HttpResponse(settings.APP_VERSION)
