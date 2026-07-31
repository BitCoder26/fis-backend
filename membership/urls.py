from django.urls import path
from .api import EnrolmentApplicationView
from . import api_views
from .views import submit_enrolment
from . import stripe_views
from .views import version_view


urlpatterns = [
    path("apply/", EnrolmentApplicationView.as_view(), name="enrolment-apply"),
    path("submit-application/", api_views.submit_application, name="submit_application"),
    path("submit-enrolment/", submit_enrolment, name="submit_enrolment"),

    # Stripe
    path("payments/<int:payment_id>/create-checkout/", stripe_views.create_checkout_session, name="create_checkout_session"),
    path("donations/create-checkout/", stripe_views.create_donation_checkout_session, name="create_donation_checkout_session"),
    path("stripe/webhook/", stripe_views.stripe_webhook, name="stripe_webhook"),
    path("version/", version_view),
]
