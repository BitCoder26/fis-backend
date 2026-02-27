from django.db import models
from django.utils import timezone
from django.db.models import Q
from django.conf import settings

# --------------------------------------
# --------------- PARTY ----------------
# --------------------------------------
class Party(models.Model):
    party_id = models.BigAutoField(primary_key=True)
    PARTY_TYPES = [
        ("individual", "individual"),
        ("organisation", "organisation"),
    ]
    party_type = models.CharField(max_length=32, choices=PARTY_TYPES)
    display_name = models.TextField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    postcode = models.TextField(blank=True, null=True)

    def __str__(self):
        name = (self.display_name or "").strip()
        return f"{self.party_id} - {name}" if name else f"Party {self.party_id}"

    class Meta:
        verbose_name = "Party"
        verbose_name_plural = "Parties"


class Individual(models.Model):
    party = models.OneToOneField(Party, on_delete=models.CASCADE, primary_key=True, related_name="individual")
    first_name = models.TextField(blank=True, null=True)
    last_name = models.TextField(blank=True, null=True)
    preferred_pronouns = models.TextField(blank=True, null=True)
    email_address = models.EmailField(blank=True, null=True)
    phone_number = models.TextField(blank=True, null=True)
    preferred_contact_method = models.TextField(blank=True, null=True)
    age_range = models.TextField(blank=True, null=True)
    ethnic_background = models.TextField(blank=True, null=True)
    ethnic_background_other = models.TextField(blank=True, null=True)
    disability_health_condition = models.TextField(blank=True, null=True)
    how_did_you_hear_about_fis = models.TextField(blank=True, null=True)
    why_join_fis = models.TextField(blank=True, null=True)
    digital_signature = models.TextField(blank=True, null=True)
    date_signed = models.DateField(blank=True, null=True)
    volunteer_occupation_current_role = models.TextField(blank=True, null=True)
    volunteer_areas_of_expertise = models.TextField(blank=True, null=True)
    volunteer_areas_of_expertise_other = models.TextField(blank=True, null=True)
    volunteer_why_volunteer = models.TextField(blank=True, null=True)
    volunteer_time_availability = models.TextField(blank=True, null=True)

    def __str__(self):
        name = f"{(self.first_name or '').strip()} {(self.last_name or '').strip()}".strip()
        return name or f"Individual {self.party_id}"

    class Meta:
        verbose_name = "Party - Individual"
        verbose_name_plural = "Party - Individuals"


class Organisation(models.Model):
    party = models.OneToOneField(Party, on_delete=models.CASCADE, primary_key=True, related_name="organisation",)
    org_name = models.TextField(blank=True, null=True)
    website_social = models.TextField(blank=True, null=True)
    org_type = models.TextField(blank=True, null=True)
    org_type_other = models.TextField(blank=True, null=True)
    company_charity_number = models.TextField("Company/Charity Number", blank=True, null=True)
    mission_purpose = models.TextField(blank=True, null=True)
    service_areas = models.TextField(blank=True, null=True)
    visibility_preference = models.TextField(blank=True, null=True)
    logo_file = models.FileField(upload_to="organisations/logos/", blank=True, null=True)
    org_activity_description = models.TextField(blank=True, null=True)
    food_system_alignment = models.TextField(blank=True, null=True)

    def __str__(self):
        name = (self.org_name or "").strip()
        return name or f"Organisation {self.party_id}"

    class Meta:
        verbose_name = "Party - Organisation"
        verbose_name_plural = "Party - Organisations"


# --------------------------------------
# ---------------- PLAN ----------------
# --------------------------------------
class Plan(models.Model):
    plan_id = models.BigAutoField(primary_key=True)
    name = models.TextField()
    type_code = models.CharField(max_length=2)   # e.g. IA/IL/PS/...
    class_code = models.CharField(max_length=2)  # e.g. CM/NM
    duration = models.TextField(blank=True, null=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    share_capital = models.IntegerField(default=0)
    active = models.BooleanField(default=True)
    declaration_set = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.plan_id} - {self.name}"

    class Meta:
        verbose_name = "Plan"
        verbose_name_plural = "Plans"
        indexes = [
            models.Index(fields=["active"]),
            models.Index(fields=["class_code", "type_code"]),
        ]


# --------------------------------------
# ------------ APPLICATIONS ------------
# --------------------------------------
class Application(models.Model):
    application_id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField(auto_now_add=True)
    plan = models.CharField(max_length=64)  # e.g. "CM-IA"
    data = models.JSONField()
    logo_file = models.FileField(upload_to="organisations/logos/", null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    turnstile_success = models.BooleanField(default=False)
    turnstile_error_codes = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=32, default="received")  # received/processed/rejected
    application_submitted_email_sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.plan} @ {self.created_at:%Y-%m-%d %H:%M}"

    class Meta:
        verbose_name = "ADMIN - Application"
        verbose_name_plural = "ADMIN - Applications"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
        ]


# --------------------------------------
# ------------ ENROLMENTS -------------
# --------------------------------------
class Enrolment(models.Model):
    enrolment_id = models.BigAutoField(primary_key=True)
    party = models.ForeignKey(Party, on_delete=models.CASCADE, related_name="enrolments")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="enrolments")
    created_at = models.DateTimeField(auto_now_add=True)
    enrolment_code = models.CharField(max_length=32, blank=True, null=True, db_index=True)
    enrolment_class = models.TextField(blank=True, null=True)
    investment_amount_pence = models.PositiveIntegerField(blank=True, null=True, db_index=True)
    STATUSES = [
        ("submitted", "Submitted"),               # user submitted, awaiting admin review, created automatically when form submitted
        ("approved_pending_payment", "Approved - pending payment"), # admin approved, serial code generated, payment request email sent, stripe link sent
        ("active", "Active"),                     # payment succeeded, enrollment fully approved, welcome email sent
        ("rejected", "Rejected"),                 # admin declined, rejection email sent
        ("cancelled", "Cancelled"),               # user cancelled later OR admin revoked membership
    ]
    status = models.CharField(max_length=40, choices=STATUSES, default="submitted")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_enrolments")
    approved_at = models.DateTimeField(blank=True, null=True)
    date_start = models.DateField(blank=True, null=True)
    date_end = models.DateField(blank=True, null=True)
    serial_number = models.IntegerField(blank=True, null=True)
    agreed_fee = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    contact_email = models.EmailField(null=True, blank=True)
    payment_requested_email_sent_at = models.DateTimeField(null=True, blank=True)
    membership_activated_email_sent_at = models.DateTimeField(null=True, blank=True)
    approved_email_sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.enrolment_code or f"Enrolment {self.enrolment_id}"
    
    def get_contact_email(self) -> str | None:
        if self.contact_email:
            return self.contact_email.strip() or None

        # derive from Party subtype if not stored
        try:
            if self.party.party_type == "individual" and hasattr(self.party, "individual"):
                return (self.party.individual.email_address or "").strip() or None
            if self.party.party_type == "organisation" and hasattr(self.party, "organisation"):
                # get nominated rep email
                rep = (self.party.organisation.nominated_reps.filter(date_end__isnull=True).order_by("-date_start").first())
                if rep and rep.email_address:
                    return rep.email_address.strip() or None
        except Exception:
            pass

        return None
    
    def investment_amount_display(self):
        return f"£{(self.investment_amount_pence or 0)/100:.2f}"

    def get_active_declaration_form(self):
        if not self.plan or not self.plan.declaration_set:
            return None
        return (
            DeclarationForm.objects
            .filter(declaration_set=self.plan.declaration_set, is_active=True)
            .order_by("-created_at")
            .first()
        )

    def required_payment_purposes(self):
        required = []

        fee_pence = 0
        if self.agreed_fee is not None:
            fee_pence = int(round(float(self.agreed_fee) * 100))
        elif self.plan and self.plan.cost is not None:
            fee_pence = int(round(float(self.plan.cost) * 100))

        if fee_pence > 0:
            required.append("membership_fee")

        if self.investment_amount_pence and self.investment_amount_pence > 0:
            required.append("share_capital")

        return required

    def is_fully_paid(self):
        required = self.required_payment_purposes()
        for purpose in required:
            if not self.payments.filter(purpose=purpose, status="succeeded").exists():
                return False
        return True

    class Meta:
        verbose_name = "Enrolment"
        verbose_name_plural = "Enrolments"
        constraints = [
            models.UniqueConstraint(
                fields=["enrolment_code"],
                name="uniq_enrolment_code",
                condition=models.Q(enrolment_code__isnull=False),
            )
        ]


class EnrolmentPayment(models.Model):
    STATUS = [
        ("requested", "requested"),
        ("pending", "pending"),
        ("succeeded", "succeeded"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("refunded", "refunded"),
        ("part_refunded", "part_refunded"),
    ]
    payment_id = models.BigAutoField(primary_key=True)
    enrolment = models.ForeignKey(Enrolment, on_delete=models.PROTECT, related_name="payments", blank=True, null=True)
    status = models.CharField(max_length=40, choices=STATUS, default="requested", db_index=True)
    PURPOSES = [
        ("membership_fee", "membership_fee"),
        ("share_capital", "share_capital"),
        ("other", "other"),
        ("refund", "refund"),
    ]
    purpose = models.CharField(max_length=32, choices=PURPOSES, default="membership_fee", db_index=True)
    amount_pence = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="GBP")
    payment_method = models.TextField(blank=True, null=True)
    requested_at = models.DateTimeField(blank=True, null=True)
    due_date = models.DateField(blank=True, null=True)
    provider = models.CharField(max_length=32, default="stripe")
    provider_ref = models.CharField(max_length=128, blank=True, null=True, db_index=True)
    provider_event_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    stripe_checkout_session_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    provider_payload = models.JSONField(blank=True, null=True)
    covers_start = models.DateField(blank=True, null=True)
    covers_end = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True, null=True)
    checkout_url = models.URLField(max_length=2048, blank=True, null=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        if self.amount_pence is not None:
            amount_display = f"£{self.amount_pence / 100:.2f}"
        else:
            amount_display = "£0.00"
        return f"Payment {self.payment_id} — {amount_display}"
    
    class Meta:
        verbose_name = "Enrolment Payment"
        verbose_name_plural = "Enrolment Payments"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["enrolment", "status"]),
            models.Index(fields=["purpose", "status"]),
        ]


# --------------------------------------
# --------- DECLARATION FORMS ----------
# --------------------------------------
class DeclarationForm(models.Model):
    form_id = models.BigAutoField(primary_key=True)
    declaration_set = models.TextField()
    version = models.TextField()
    content = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=False)
    ended_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        status = "active" if self.is_active else "inactive"
        return f"{self.declaration_set} — {self.version} ({status})"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["declaration_set"],
                condition=Q(is_active=True),
                name="uniq_active_declarationform_per_set",
            )
        ]

        verbose_name = "Declaration Form"
        verbose_name_plural = "Declaration Forms"
        indexes = [
            models.Index(fields=["declaration_set", "is_active"]),
        ]
    
    def deactivate(self, when=None):
        """Optional helper."""
        when = when or timezone.now()
        self.is_active = False
        if self.ended_at is None:
            self.ended_at = when


class Declaration(models.Model):
    declarations_id = models.BigAutoField(primary_key=True)
    enrolment = models.ForeignKey(Enrolment, on_delete=models.CASCADE, related_name="declarations")
    form = models.ForeignKey(DeclarationForm, on_delete=models.PROTECT, related_name="declarations")
    is_current = models.BooleanField(default=False)
    approved_at = models.DateTimeField(blank=True, null=True)
    conflict_of_interest = models.BooleanField(blank=True, null=True)
    conflict_of_interest_details = models.TextField(blank=True, null=True)
    conflict_of_interest_details_accurate_info_declaration = models.BooleanField(blank=True, null=True)
    data_contact_consent = models.BooleanField(blank=True, null=True)
    data_marketing_consent = models.BooleanField(blank=True, null=True)
    commitment_accepted = models.BooleanField(blank=True, null=True)
    ind_voting_share_acknowledged = models.BooleanField(blank=True, null=True)
    ind_voting_eligibility_confirmed = models.BooleanField(blank=True, null=True)
    org_voting_rights_terms_accepted = models.BooleanField(blank=True, null=True)

    def __str__(self):
        code = getattr(self.enrolment, "enrolment_code", None) or "-"
        name = getattr(self.enrolment.party, "display_name", None) or "-"
        return f"{self.declarations_id} — {code} — {name}"

    class Meta:
        verbose_name = "Declaration"
        verbose_name_plural = "Declarations"
        indexes = [
            models.Index(fields=["is_current"]),
            models.Index(fields=["approved_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["enrolment"],
                condition=models.Q(is_current=True),
                name="uniq_current_declaration_per_enrolment",
            )
        ]


# --------------------------------------
# -------- ORG HISTORY / ROLES ---------
# --------------------------------------
class NominatedRep(models.Model):
    nominated_rep_id = models.BigAutoField(primary_key=True)
    org_party = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="nominated_reps")
    first_name = models.TextField(blank=True, null=True)
    surname = models.TextField(blank=True, null=True)
    position_role = models.TextField(blank=True, null=True)
    email_address = models.EmailField(blank=True, null=True)
    phone_number = models.TextField(blank=True, null=True)
    preferred_contact_method = models.TextField(blank=True, null=True)
    authorisation_confirmation = models.BooleanField(blank=True, null=True)
    date_start = models.DateField()
    date_end = models.DateField(blank=True, null=True)

    def __str__(self):
        org_name = (self.org_party.org_name or "").strip() or "(no name)"
        return f"{org_name} — {self.first_name or ''} {self.surname or ''}".strip()

    class Meta:
        verbose_name = "Org - Nominated Rep"
        verbose_name_plural = "Org - Nominated Reps"
        indexes = [
            models.Index(fields=["date_end"]),
        ]


class AuthorisedSignatory(models.Model):
    authorised_signatory_id = models.BigAutoField(primary_key=True)
    org_party = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="authorised_signatories")
    first_name = models.TextField(blank=True, null=True)
    surname = models.TextField(blank=True, null=True)
    position = models.TextField(blank=True, null=True)
    digital_signature = models.TextField(blank=True, null=True)
    date_signed = models.DateField(blank=True, null=True)
    date_start = models.DateField()
    date_end = models.DateField(blank=True, null=True)

    def __str__(self):
        org_name = (self.org_party.org_name or "").strip() or "(no name)"
        name = f"{(self.first_name or '').strip()} {(self.surname or '').strip()}".strip() or "Unnamed signatory"
        return f"{name} — {org_name}"

    class Meta:
        verbose_name = "Org - Authorised Signatory"
        verbose_name_plural = "Org - Authorised Signatories"
        indexes = [
            models.Index(fields=["date_end"]),
        ]