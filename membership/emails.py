from django.conf import settings
from django.core.mail import send_mail


def send_application_submitted_email(*, to_email: str, plan_name: str | None = None, enrolment_code: str | None = None) -> None:
    subject = "We received your membership application"
    lines = ["Thanks for applying to the Food Investors Society." + " " + "We've received your application and will review it shortly."]
    
    if plan_name:
        lines += ["", f"Plan: {plan_name}"]
    if enrolment_code:
        lines += [f"Reference: {enrolment_code}"]

    lines += ["", "If approved, you'll receive a payment link.", " ", "— The Food Investors Society"]
    send_mail(
        subject=subject, message="\n".join(lines), from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[to_email], fail_silently=False
    )

def send_payment_requested_email(*, to_email: str, enrolment_code: str, checkout_url: str, amount_pence: int | None = None) -> None:
    subject = "Payment link for your Food Investors Society membership"
    amount_line = f"Amount: £{amount_pence/100:.2f}" if amount_pence is not None else None
    lines = ["Your membership application has been approved.", " ", f"Reference: {enrolment_code}"]
    
    if amount_line:
        lines += [amount_line]

    lines += [
        "", "Please complete your payment using this secure link:", checkout_url, "",
        "If you have any issues, reply to this email.", " ", "— The Food Investors Society",
    ]
    send_mail(subject, "\n".join(lines), settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=False)


def send_membership_activated_email(*, to_email: str, enrolment_code: str, plan_name: str | None = None) -> None:
    subject = "Your Food Investors Society membership is now active"
    lines = ["Your payment has been received and your membership is now active.", " ", f"Reference: {enrolment_code}"]
    
    if plan_name:
        lines += [f"Plan: {plan_name}"]

    lines += ["", "Welcome aboard!", "", "— The Food Investors Society"]
    send_mail(subject, "\n".join(lines), settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=False)