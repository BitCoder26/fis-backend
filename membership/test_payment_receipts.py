from contextlib import nullcontext
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from membership.emails import send_application_submitted_email, send_membership_payment_received_email
from membership.stripe_views import _handle_checkout_paid


class MembershipPaymentEmailTests(SimpleTestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_application_email_does_not_expose_an_internal_database_id(self):
        from django.core import mail

        send_application_submitted_email(
            to_email="member@example.com",
            plan_name="CM-IA",
            enrolment_code="44",
            plan_code="CM-IA",
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("Reference:", mail.outbox[0].body)
        self.assertNotIn("44", mail.outbox[0].body)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_application_email_can_show_an_assigned_public_reference(self):
        from django.core import mail

        send_application_submitted_email(
            to_email="member@example.com",
            plan_name="Individual Annual",
            enrolment_code="CM-IA-0002",
            plan_code="CM-IA",
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Reference: CM-IA-0002", mail.outbox[0].body)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_email_contains_payment_and_member_facing_enrolment_number(self):
        from django.core import mail

        send_membership_payment_received_email(
            to_email="member@example.com",
            enrolment_code="CM-IA-0002",
            amount_pence=100,
            paid_on="28 July 2026",
            plan_name="Individual Annual",
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Payment received", mail.outbox[0].subject)
        self.assertIn("Amount paid: £1.00", mail.outbox[0].body)
        self.assertIn("Membership number: CM-IA-0002", mail.outbox[0].body)
        self.assertIn("Payment date: 28 July 2026", mail.outbox[0].body)


class MembershipPaymentWebhookTests(SimpleTestCase):
    @patch("membership.stripe_views.transaction.atomic", return_value=nullcontext())
    @patch("membership.stripe_views.transaction.on_commit", side_effect=lambda callback: callback())
    @patch("membership.stripe_views.send_membership_payment_received_email")
    @patch("membership.stripe_views._find_payment_for_session")
    def test_successful_membership_payment_sends_receipt_once(
        self,
        find_payment,
        send_receipt,
        _on_commit,
        _atomic,
    ):
        plan = Mock(name="plan")
        plan.name = "Individual Annual"

        enrolment = Mock(name="enrolment")
        enrolment.status = "approved_pending_payment"
        enrolment.date_start = None
        enrolment.enrolment_code = "CM-IA-0002"
        enrolment.enrolment_id = 39
        enrolment.plan = plan
        enrolment.plan_id = 4
        enrolment.membership_activated_email_sent_at = None
        enrolment.is_fully_paid.return_value = True
        enrolment.get_contact_email.return_value = "member@example.com"

        payment = Mock(name="payment")
        payment.status = "pending"
        payment.purpose = "membership_fee"
        payment.amount_pence = 100
        payment.currency = "GBP"
        payment.provider_ref = None
        payment.stripe_payment_intent_id = None
        payment.paid_at = None
        payment.enrolment = enrolment
        payment.enrolment_id = 39
        payment.donor_email = None
        find_payment.return_value = payment

        event = {"id": "evt_test", "type": "checkout.session.completed"}
        session = {
            "id": "cs_test_123",
            "payment_status": "paid",
            "payment_intent": "pi_test_123",
            "amount_total": 100,
            "currency": "gbp",
            "customer": "cus_test_123",
        }

        _handle_checkout_paid(event, session)

        send_receipt.assert_called_once()
        kwargs = send_receipt.call_args.kwargs
        self.assertEqual(kwargs["to_email"], "member@example.com")
        self.assertEqual(kwargs["enrolment_code"], "CM-IA-0002")
        self.assertEqual(kwargs["amount_pence"], 100)
        self.assertEqual(enrolment.status, "active")
        self.assertIsNotNone(enrolment.membership_activated_email_sent_at)

        _handle_checkout_paid(event, session)
        send_receipt.assert_called_once()
