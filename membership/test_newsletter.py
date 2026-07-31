from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings

from membership.emails import submit_newsletter_opt_in
from membership.views import _submit_newsletter_opt_in


class BrevoNewsletterFormTests(SimpleTestCase):
    @override_settings(BREVO_API_KEY="test-key", BREVO_NEWSLETTER_LIST_ID=2)
    @patch("membership.emails.requests.post")
    def test_posts_expected_contact_fields(self, post):
        response = Mock()
        post.return_value = response

        submit_newsletter_opt_in(email="applicant@example.com")

        post.assert_called_once_with(
            "https://api.brevo.com/v3/contacts",
            json={
                "email": "applicant@example.com",
                "listIds": [2],
                "updateEnabled": True,
            },
            headers={
                "api-key": "test-key",
                "accept": "application/json",
                "content-type": "application/json",
            },
            timeout=10,
        )
        response.raise_for_status.assert_called_once_with()

    @patch("membership.views.submit_newsletter_opt_in")
    def test_submits_only_when_marketing_consent_is_true(self, submit):
        self.assertTrue(
            _submit_newsletter_opt_in(
                email="applicant@example.com",
                consent_marketing="on",
            )
        )
        submit.assert_called_once_with(email="applicant@example.com")

        submit.reset_mock()
        self.assertFalse(
            _submit_newsletter_opt_in(
                email="applicant@example.com",
                consent_marketing="off",
            )
        )
        submit.assert_not_called()

    @patch("membership.views.submit_newsletter_opt_in")
    def test_brevo_failure_does_not_escape(self, submit):
        submit.side_effect = requests.RequestException("Brevo unavailable")

        self.assertFalse(
            _submit_newsletter_opt_in(
                email="applicant@example.com",
                consent_marketing=True,
            )
        )
