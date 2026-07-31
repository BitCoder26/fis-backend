import requests

from django.conf import settings
from django.core.mail import send_mail


def submit_newsletter_opt_in(*, email: str) -> None:
    """
    Add (or update) a contact on the Newsletter list via Brevo's Contacts API.

    Brevo's hosted form endpoint (sibforms.com) silently drops server-side
    submissions despite returning {"success": true} - likely bot detection on
    non-browser traffic. The Contacts API is a real authenticated call, so it
    doesn't have that problem.
    """
    response = requests.post(
        "https://api.brevo.com/v3/contacts",
        json={
            "email": email,
            "listIds": [settings.BREVO_NEWSLETTER_LIST_ID],
            "updateEnabled": True,
        },
        headers={
            "api-key": settings.BREVO_API_KEY,
            "accept": "application/json",
            "content-type": "application/json",
        },
        timeout=10,
    )
    response.raise_for_status()


def _send_via_brevo(*, template_id, to_email, params, to_name=None) -> bool:
    """
    Send a Brevo transactional *template* email.

    Returns True if the send was handed off to Brevo; returns False (without
    raising) when Brevo isn't configured, so callers can fall back to Django's
    send_mail. Network/API errors are raised so the caller can log them.
    """
    if not settings.BREVO_API_KEY or not template_id:
        return False

    recipient = {"email": to_email}
    if to_name:
        recipient["name"] = to_name

    resp = requests.post(
        settings.BREVO_API_URL,
        json={
            "sender": {"email": settings.BREVO_SENDER_EMAIL, "name": settings.BREVO_SENDER_NAME},
            "to": [recipient],
            "templateId": int(template_id),
            "params": params,
        },
        headers={
            "api-key": settings.BREVO_API_KEY,
            "accept": "application/json",
            "content-type": "application/json",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return True


def send_donation_receipt_email(
    *,
    to_email: str,
    donor_name: str | None,
    amount_pence: int | None,
    reference: str,
    currency: str = "GBP",
    donated_on: str | None = None,
) -> None:
    """
    Receipt emailed to a donor after a successful Stripe donation.

    Prefers the Brevo transactional template (authenticated sender, designed in
    Brevo's editor); falls back to a plain-text Django email if Brevo isn't set.
    Params passed to the template: donor_name, amount, reference, currency, date.
    """
    ccy = (currency or "GBP").upper()
    symbol = "£" if ccy == "GBP" else f"{ccy} "
    amount = f"{symbol}{(amount_pence or 0) / 100:.2f}"
    name = (donor_name or "").strip() or "there"
    date_str = donated_on or ""

    params = {
        "donor_name": name,
        "amount": amount,
        "reference": reference,
        "currency": ccy,
        "date": date_str,
    }

    # Preferred path: Brevo transactional template.
    if _send_via_brevo(
        template_id=settings.BREVO_DONATION_RECEIPT_TEMPLATE_ID,
        to_email=to_email,
        params=params,
        to_name=name,
    ):
        return

    # Fallback: plain-text via the configured Django EMAIL_BACKEND.
    subject = "Thank you for your donation to The Food Investors Society"
    lines = [
        f"Dear {name},",
        "",
        "Thank you for your generous donation to The Food Investors Society.",
        "",
        f"Amount: {amount}",
        f"Reference: {reference}",
    ]
    if date_str:
        lines.append(f"Date: {date_str}")
    lines += [
        "",
        "Your support helps us build a fairer, healthier food future.",
        "",
        "— The Food Investors Society",
    ]
    send_mail(subject, "\n".join(lines), settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=False)


def send_membership_payment_received_email(
    *,
    to_email: str,
    enrolment_code: str,
    amount_pence: int | None,
    currency: str = "GBP",
    paid_on: str | None = None,
    plan_name: str | None = None,
    plan_code: str | None = None,
    marketing_consent: bool = False,
) -> None:
    """Confirm payment and registration after a successful member checkout."""
    ccy = (currency or "GBP").upper()
    symbol = "£" if ccy == "GBP" else f"{ccy} "
    amount = f"{symbol}{(amount_pence or 0) / 100:.2f}"
    label = "enrolment" if _is_non_member(
        plan_code=plan_code,
        enrolment_code=enrolment_code,
        plan_name=plan_name,
    ) else "membership"

    if _send_via_brevo(
        template_id=settings.BREVO_MEMBERSHIP_PAYMENT_RECEIVED_TEMPLATE_ID,
        to_email=to_email,
        params={
            "label": label,
            "amount": amount,
            "reference": enrolment_code,
            "plan_name": plan_name or "",
            "paid_on": paid_on or "",
            "marketing_consent": marketing_consent,
        },
    ):
        return

    subject = f"Payment received - your Food Investors Society {label} is active"
    lines = [
        "Thank you. We have received your payment.",
        "",
        f"Amount paid: {amount}",
        f"{label.capitalize()} number: {enrolment_code}",
    ]
    if plan_name:
        lines.append(f"Plan: {plan_name}")
    if paid_on:
        lines.append(f"Payment date: {paid_on}")

    lines += [
        "",
        f"Your Food Investors Society {label} is now active.",
    ]
    if marketing_consent:
        lines += ["", "You've also been added to our newsletter for updates."]
    lines += [
        "",
        "— The Food Investors Society",
    ]
    send_mail(subject, "\n".join(lines), settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=False)


def _is_non_member(*, plan_code: str | None = None, enrolment_code: str | None = None, plan_name: str | None = None) -> bool:
    code = (plan_code or "").strip().upper()
    if code.startswith("NM-"):
        return True
    if code.startswith("CM-"):
        return False

    ref = (enrolment_code or "").strip().upper()
    if ref.startswith("NM-"):
        return True
    if ref.startswith("CM-"):
        return False

    # fallback: if plan_name itself is a code-like value
    name = (plan_name or "").strip().upper()
    return name.startswith("NM-")


def send_application_submitted_email(
    *,
    to_email: str,
    plan_name: str | None = None,
    enrolment_code: str | None = None,
    plan_code: str | None = None,
) -> None:
    label = "enrolment" if _is_non_member(plan_code=plan_code, enrolment_code=enrolment_code, plan_name=plan_name) else "membership"
    # Never expose an internal database primary key as the applicant's
    # reference. The public CM-/NM- code is assigned when the application is
    # approved and will appear in the payment email.
    reference = enrolment_code if enrolment_code and enrolment_code.upper().startswith(("CM-", "NM-")) else ""

    if _send_via_brevo(
        template_id=settings.BREVO_APPLICATION_SUBMITTED_TEMPLATE_ID,
        to_email=to_email,
        params={"label": label, "plan_name": plan_name or "", "reference": reference},
    ):
        return

    subject = f"We received your {label} application"
    lines = ["Thanks for applying to the Food Investors Society." + " " + "We've received your application and will review it shortly."]

    if plan_name:
        lines += ["", f"Plan: {plan_name}"]
    if reference:
        lines += [f"Reference: {reference}"]

    lines += ["", "If approved, you'll receive a payment link.", " ", "— The Food Investors Society"]
    send_mail(
        subject=subject, message="\n".join(lines), from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[to_email], fail_silently=False
    )

def send_payment_requested_email(
    *,
    to_email: str,
    enrolment_code: str,
    checkout_url: str,
    amount_pence: int | None = None,
    plan_code: str | None = None,
) -> None:
    label = "enrolment" if _is_non_member(plan_code=plan_code, enrolment_code=enrolment_code) else "membership"
    amount = f"£{amount_pence/100:.2f}" if amount_pence is not None else ""

    if _send_via_brevo(
        template_id=settings.BREVO_PAYMENT_REQUESTED_TEMPLATE_ID,
        to_email=to_email,
        params={"label": label, "reference": enrolment_code, "amount": amount, "checkout_url": checkout_url},
    ):
        return

    subject = f"Payment link for your Food Investors Society {label}"
    lines = [f"Your {label} application has been approved.", " ", f"Reference: {enrolment_code}"]

    if amount:
        lines += [f"Amount: {amount}"]

    lines += [
        "", "Please complete your payment using this secure link:", checkout_url, "",
        "— The Food Investors Society",
    ]
    send_mail(subject, "\n".join(lines), settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=False)


def send_membership_activated_email(
    *,
    to_email: str,
    enrolment_code: str,
    plan_name: str | None = None,
    plan_code: str | None = None,
    marketing_consent: bool = False,
) -> None:
    label = "enrolment" if _is_non_member(plan_code=plan_code, enrolment_code=enrolment_code, plan_name=plan_name) else "membership"

    if _send_via_brevo(
        template_id=settings.BREVO_MEMBERSHIP_ACTIVATED_TEMPLATE_ID,
        to_email=to_email,
        params={
            "label": label,
            "reference": enrolment_code,
            "plan_name": plan_name or "",
            "marketing_consent": marketing_consent,
        },
    ):
        return

    subject = f"Your Food Investors Society {label} is now active"
    lines = [f"Your application has been approved and your {label} is now active.", " ", f"Reference: {enrolment_code}"]

    if plan_name:
        lines += [f"Plan: {plan_name}"]

    lines += ["", "Welcome aboard!"]
    if marketing_consent:
        lines += ["", "You've also been added to our newsletter for updates."]
    lines += ["", "— The Food Investors Society"]
    send_mail(subject, "\n".join(lines), settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=False)
