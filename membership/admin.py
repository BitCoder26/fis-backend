from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from datetime import timedelta
from django.db import connection
from datetime import date as date_cls
from .models import Payment
from .emails import send_payment_requested_email, send_membership_activated_email
from .stripe_service import ensure_checkout_session
import json
import os

from .models import (
    Party,
    Individual,
    Organisation,
    Enrolment,
    Payment,
    Plan,
    Declaration,
    DeclarationForm,
    AuthorisedSignatory,
    NominatedRep,
    Application,
)

CLASS_CODE_TO_CATEGORY = {
    "CM": "community_member",
    "NM": "non_member",
}

def _normalize_submission_keys(d: dict) -> dict:
    """
    - Convert kebab-case keys to snake_case
    - Keep existing snake_case
    - Optionally map legacy aliases to your canonical names
    """
    if not d:
        return {}

    out = {}
    for k, v in d.items():
        if isinstance(k, str):
            out[k.replace("-", "_")] = v
        else:
            out[k] = v

    # Optional: alias mapping (if you want ONE canonical name everywhere)
    alias = {
        "conflict_details": "conflict_of_interest_details",
        # add more if you want...
    }
    for old, new in alias.items():
        if old in out and new not in out:
            out[new] = out[old]

    return out

@admin.action(description="Cancel enrolment(s) (set status + end date)")
def cancel_enrolments(modeladmin, request, queryset):
    today = timezone.now().date()

    to_cancel = queryset.exclude(status="cancelled").filter(date_end__isnull=True)

    updated = to_cancel.update(
        status="cancelled",
        date_end=today,
    )

    modeladmin.message_user(
        request,
        f"Cancelled {updated} enrolment(s).",
        level=messages.SUCCESS if updated else messages.WARNING,
    )

def _agreed_fee_for_plan(plan: Plan, d: dict):
    """
    Returns the agreed annual/lifetime fee (in pounds) for the Enrolment row.
    - For annual plans: use user selection if provided, else plan.cost
    - For lifetime plans: use plan.cost (or fallback)
    - For free plans: 0
    """
    type_code = (getattr(plan, "type_code", "") or "").strip().upper()
    class_code = (getattr(plan, "class_code", "") or "").strip().upper()

    if class_code == "NM":
        return 0
    
    if type_code in {"IA"}:
        amt = _money_from_submission(d)
        if amt is None:
            amt = getattr(plan, "cost", None)
        return int(amt) if amt is not None else None

    if type_code in {"IL", "PS", "CS"}:
        amt = getattr(plan, "cost", None)
        if amt is None and type_code == "IL":
            amt = 100
        return int(amt) if amt is not None else None

    return None

def _parse_date(s):
    if not s:
        return None
    try:
        return date_cls.fromisoformat(str(s))
    except ValueError:
        return None

def _to_bool(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "y", "1", "on"):
            return True
        if s in ("false", "no", "n", "0", "off"):
            return False
        if s in ("", "none", "null"):
            return None
    return None

def _get_active_declaration_form_for_plan(plan: Plan) -> DeclarationForm | None:
    declaration_set = getattr(plan, "declaration_set", None)
    if not declaration_set:
        return None

    return (
        DeclarationForm.objects
        .filter(declaration_set=declaration_set, is_active=True, ended_at__isnull=True)
        .order_by("-created_at")
        .first()
    )

def _money_from_submission(d: dict) -> int | None:
    """
    Parse contribution amount if present.
    Returns an int (e.g., pounds) or None.
    Adjust if your Enrolment model stores pennies.
    """
    choice = (d.get("contribution_choice") or "").strip()
    other = (d.get("contribution_other_amount") or "").strip()

    if choice.isdigit():
        return int(choice)
    if choice == "other" and other.isdigit():
        return int(other)

    # org partner plans might have fixed fees and no field
    return None


@admin.action(description="Process submissions (create Party + Enrolment records)")
def process_submissions(modeladmin, request, queryset):
    today = timezone.now().date()

    processed = 0
    skipped = 0
    failed = 0

    # Only handle "received"/"approved"
    queryset = queryset.exclude(status__in=["processed"])

    for sub in queryset:
        try:
            with transaction.atomic():
                sub = Application.objects.select_for_update().get(pk=sub.pk)

                if sub.status == "processed":
                    skipped += 1
                    continue

                plan_key = (sub.plan or "").strip().upper()
                try:
                    cc, tc = plan_key.split("-", 1)
                except ValueError:
                    raise ValueError(f"plan must look like 'CM-IA' but got: {plan_key}")

                plan = Plan.objects.get(class_code=cc, type_code=tc, active=True)

                d = _normalize_submission_keys(sub.data or {})  
                d.pop("cf_turnstile_response", None)

                is_org = tc in {"CN", "CF"} or bool((d.get("org_name") or "").strip())

                # 2) Create Party
                party = Party.objects.create(
                    party_type=("organisation" if is_org else "individual"),
                    display_name=(d.get("org_name") if is_org else f"{d.get('first_name','')} {d.get('surname','')}".strip()),
                    address=d.get("registered_address") if is_org else d.get("address"),
                    postcode=d.get("postcode"),
                )

                # 3) Create subtype row
                if is_org:
                    org = Organisation.objects.create(
                        party=party,
                        org_name=d.get("org_name", ""),
                        org_type=d.get("org_type", ""),
                        org_type_other=d.get("other_org_type") or d.get("org_type_other") or "",
                        company_charity_number=(
                            d.get("company_charity_number")
                            or d.get("company_number")
                            or ""
                        ),
                        website_social=d.get("website_social", ""),
                        mission_purpose=d.get("mission_statement"),
                        service_areas=d.get("local_areas_you_serve"),
                        org_activity_description=(
                            d.get("what_you_do_who_you_support")
                            or d.get("what_you_do_approach_good_food")
                            or d.get("org_what_you_do")
                            or ""
                        ),
                        food_system_alignment=(
                            d.get("people_access_good_food")
                            or d.get("food_system")
                            or d.get("org_alignment")
                            or ""
                        ),
                        visibility_preference=d.get("visibility_preference"),
                    )
                    # Attach logo if present (re-use the uploaded file)
                    if sub.logo_file:
                        org.logo_file.save(os.path.basename(sub.logo_file.name), sub.logo_file, save=True )

                    NominatedRep.objects.create(
                        org_party=org,
                        first_name=(d.get("primary_first_name") or d.get("first_name") or "").strip(),
                        surname=(d.get("primary_surname") or d.get("surname") or "").strip(),
                        email_address=(d.get("email") or "").strip(),
                        phone_number=(d.get("phone") or "").strip(),
                        position_role=(d.get("position_within_org") or "").strip(),
                        preferred_contact_method=(d.get("preferred_contact_method") or d.get("contact_method") or "").strip() or None,
                        date_start=today,
                        date_end=None,
                    )

                    AuthorisedSignatory.objects.create(
                        org_party=org,
                        first_name=d.get("auth_first_name", ""),
                        surname=d.get("auth_surname", ""),
                        position=d.get("position", ""),
                        digital_signature=(d.get("digital_signature") or "").strip() or None,
                        date_signed=_parse_date(d.get("date")),
                        date_start=today,
                        date_end=None,
                    )

                else:
                    Individual.objects.create(
                        party=party,
                        first_name=d.get("first_name"),
                        last_name=d.get("surname"),
                        preferred_pronouns=d.get("pronouns"),

                        email_address=d.get("email"),
                        phone_number=d.get("phone"),
                        preferred_contact_method=d.get("contact_method"),
                        age_range=d.get("age_range"),

                        ethnic_background=d.get("ethnicity"),
                        ethnic_background_other=d.get("ethnicity_other"),

                        disability_health_condition=d.get("disability"),

                        how_did_you_hear_about_fis=d.get("hear_about_us") or d.get("how_hear_about_us"),
                        why_join_fis=d.get("why_join_us"),

                        digital_signature=d.get("digital_signature"),
                        date_signed=_parse_date(d.get("date")),

                        volunteer_occupation_current_role=d.get("occupation"),
                        volunteer_areas_of_expertise=d.get("area_of_expertise"),
                        volunteer_areas_of_expertise_other=d.get("area_of_expertise_other"),
                        volunteer_why_volunteer=d.get("volunteering_reasons"),
                        volunteer_time_availability=d.get("time_to_offer"),
                    )

                investment_raw = (d.get("investment_amount") or "").strip()
                investment_pence = int(investment_raw) * 100 if investment_raw.isdigit() else None

                # 4) Create Enrolment row (fixed plan, new enrolment)
                class_code, type_code = _plan_codes(plan)

                email = (d.get("email") or "").strip() if not is_org else (d.get("email") or "").strip()


                agreed_fee = _agreed_fee_for_plan(plan, d)
                enrolment = Enrolment.objects.create(
                    party=party,
                    plan=plan,
                    enrolment_class=CLASS_CODE_TO_CATEGORY.get(class_code, class_code),
                    status="submitted",
                    agreed_fee=agreed_fee,
                    investment_amount_pence=investment_pence,
                    date_start=today, 
                    contact_email=email or None,
                )

                # 5) Create Declarations row against current active form (fixed DeclarationForm table)
                form = _get_active_declaration_form_for_plan(plan)
                if form:
                    Declaration.objects.create(
                        enrolment=enrolment,
                        form=form,
                        is_current=True,
                        approved_at=None,

                        commitment_accepted=_to_bool(
                            d.get("commitment_accepted")
                            or d.get("all_commitment_accepted")
                            or d.get("volunteer_declaration_agree")
                        ),
                        conflict_of_interest=_to_bool(d.get("conflict_of_interest")),
                        conflict_of_interest_details=d.get("conflict_of_interest_details") or d.get("conflict_details"),
                        conflict_of_interest_details_accurate_info_declaration=_to_bool(d.get("conflict_agree")),
                        data_contact_consent=_to_bool(d.get("consent_contact")),
                        data_marketing_consent=_to_bool(d.get("consent_marketing")),
                        ind_voting_share_acknowledged=_to_bool(d.get("understand_share_cost")),
                        ind_voting_eligibility_confirmed=_to_bool(d.get("understand_16_over")),
                        org_voting_rights_terms_accepted=_to_bool(d.get("org_voting_rights_terms_accepted") or d.get("partnership_commitment_agree")),
                    )

                # 6) Mark submission as processed
                sub.status = "processed"
                sub.save(update_fields=["status"])

                processed += 1

        except Exception as e:
            failed += 1
            modeladmin.message_user(
                request,
                f"Submission {sub.pk} failed: {e}",
                level=messages.ERROR
            )

    modeladmin.message_user(
        request,
        f"Processed={processed}, Skipped={skipped}, Failed={failed}.",
        level=messages.SUCCESS if processed else messages.WARNING
    )

@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    actions = [process_submissions]

    list_display = (
        "application_id",
        "plan",
        "created_at",
        "status",
        "has_logo",
        "contact_name",
        "contact_email",
    )
    list_filter = ("plan", "turnstile_success", "status")
    search_fields = ("plan",)
    readonly_fields = (
        "created_at",
        "pretty_data",
        "ip_address",
        "user_agent",
        "turnstile_success",
        "turnstile_error_codes",
    )
    fieldsets = (
        ("Submission", {"fields": ("plan", "status", "created_at")}),
        ("Applicant data", {"fields": ("pretty_data",)}),
        ("Uploads", {"fields": ("logo_file",)}),
        ("Security", {"fields": ("turnstile_success", "turnstile_error_codes", "ip_address", "user_agent")}),
    )

    @admin.display(description="Name / Org")
    def contact_name(self, obj):
        d = obj.data or {}
        # volunteer / individual
        first = d.get("first_name", "")
        last = d.get("surname", "")
        # org forms may have nominated rep names too
        return (first + " " + last).strip() or d.get("org_name", "") or "-"

    @admin.display(description="Email")
    def contact_email(self, obj):
        return (obj.data or {}).get("email", "-")

    @admin.display(boolean=True, description="Logo?")
    def has_logo(self, obj):
        return bool(obj.logo_file)

    @admin.display(description="Submitted data")
    def pretty_data(self, obj):
        try:
            d = dict(obj.data or {})
            d.pop("cf_turnstile_response", None)  # hide it
            return json.dumps(d, indent=2, ensure_ascii=False)
        except Exception:
            return str(obj.data)

    class Media:
        css = {"all": ("admin/custom_admin.css",)}


def _advisory_lock(key: int = 991234567):
    """
    Postgres-only: serialises code/serial generation across concurrent requests.
    """
    with connection.cursor() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s);", [key])

def _plan_codes(plan):
    """
    Adjust these attribute names to match your Plan model.
    Common options: class_code/type_code OR party_class/party_type etc.
    """
    class_code = getattr(plan, "class_code", None) or getattr(plan, "party_class_code", None)
    type_code = getattr(plan, "type_code", None)

    if not class_code or not type_code:
        raise ValueError("Plan is missing class_code/type_code fields.")

    return str(class_code).upper(), str(type_code).upper()

@admin.action(description="Approve enrolment(s) (generate serial + code)")
def approve_enrolments(modeladmin, request, queryset):
    # Only pending ones
    pending = queryset.filter(status="submitted")

    if not pending.exists():
        modeladmin.message_user(request, "No pending enrolments selected.", level=messages.WARNING)
        return

    approved_count = 0
    skipped = 0

    with transaction.atomic():
        for enrolment in pending.select_related("plan"):
            try:
                with transaction.atomic():
                    _advisory_lock()

                    # Re-fetch & lock this enrolment row
                    m = Enrolment.objects.select_for_update().select_related("plan").get(pk=enrolment.pk)
                    if m.status != "submitted":
                        skipped += 1
                        continue

                    # Generate next serial (global across all enrolments)
                    class_code, type_code = _plan_codes(m.plan)
                    prefix = f"{class_code}-{type_code}"

                    next_serial = (
                        Enrolment.objects
                        .filter(enrolment_code__startswith=prefix + "-", serial_number__isnull=False)
                        .aggregate(mx=Max("serial_number"))["mx"]
                        or 0
                    ) + 1

                    enrolment_code = f"{prefix}-{next_serial:04d}"

                    m.serial_number = next_serial
                    m.enrolment_code = enrolment_code

                    now = timezone.now()
                    today = now.date()

                    m.approved_by = request.user
                    m.approved_at = now

                    m.status = "approved_pending_payment"                       
                    m.save()

                    # Approve declarations (current one for this enrolment)
                    Declaration.objects.filter(enrolment=m, is_current=True, approved_at__isnull=True).update(approved_at=now)

                    approved_count += 1

            except ValueError as e:
                modeladmin.message_user(request, f"Skipped enrolment {enrolment.pk}: {e}", level=messages.ERROR)
                skipped += 1
            except Exception as e:
                modeladmin.message_user(request, f"Error approving enrolment {enrolment.pk}: {e}", level=messages.ERROR)

    modeladmin.message_user(
        request,
        f"Approved {approved_count} enrolment(s). Skipped {skipped}.",
        level=messages.SUCCESS if approved_count else messages.WARNING,
    )

# ---------------------------------------
# --------- PARTIES / SUBTYPES ----------
# ---------------------------------------

class IndividualInline(admin.StackedInline):
    model = Individual
    extra = 0
    can_delete = False
    show_change_link = True

class OrganisationInline(admin.StackedInline):
    model = Organisation
    extra = 0
    can_delete = False
    show_change_link = True

class EnrolmentInline(admin.TabularInline):
    model = Enrolment
    extra = 0
    show_change_link = True

@admin.register(Party)
class PartyAdmin(admin.ModelAdmin):
    list_display = ("party_id", "party_type", "display_name", "postcode", "address")
    list_filter = ("party_type",)
    search_fields = (
        "=party_id","display_name", "postcode", "individual__first_name", "individual__last_name", 
        "individual__email_address", "organisation__org_name", "organisation__company_charity_number",
    )
    ordering = ("-party_id",)
    list_per_page = 50
    inlines = [IndividualInline, OrganisationInline, EnrolmentInline]

    class Media:
        css = {"all": ("admin/custom_admin.css",)}


@admin.register(Individual)
class IndividualAdmin(admin.ModelAdmin):
    list_display = ("party_id", "first_name", "last_name", "age_range", "email_address", "phone_number")
    list_filter = ("age_range", "preferred_contact_method")
    search_fields = ("first_name", "last_name", "email_address", "phone_number")
    autocomplete_fields = ("party",)
    fieldsets = (
        ("Core details", {"fields": (
            "party", "first_name", "last_name", "preferred_pronouns", "email_address", 
            "phone_number", "preferred_contact_method", "age_range", "ethnic_background", 
            "ethnic_background_other", "disability_health_condition", "how_did_you_hear_about_fis",
            "why_join_fis", "digital_signature", "date_signed",
        )}),
        ("Volunteer-only fields", {"fields": (
                "volunteer_occupation_current_role", "volunteer_areas_of_expertise",
                "volunteer_areas_of_expertise_other", "volunteer_why_volunteer","volunteer_time_availability",
            ),
            "classes": ("collapse",),
        }),
    )


@admin.register(Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    list_display = ("party_id", "org_name", "org_type", "company_charity_number_display", "current_rep", "current_signatory")
    list_filter = ("org_type",)
    list_select_related = ("party",)
    search_fields = ("org_name", "company_charity_number", "website_social", "party__party_id")
    autocomplete_fields = ("party",)

    class Media:
        css = {"all": ("admin/custom_admin.css",)}

    @admin.display(description="Company/Charity Number", ordering="company_charity_number")
    def company_charity_number_display(self, obj):
        return obj.company_charity_number or "-"

    @admin.display(description="Organisation ID - Name", ordering="party__party_id")
    def organisation_id_and_name(self, obj):
        # obj.party is the Party row for this Organisation
        if not obj.party:
            return "-"

        url = reverse("admin:membership_organisation_change", args=[obj.pk])
        # If your org name field is org_name (as used below), keep it:
        name = (obj.org_name or "").strip() or "(no name)"
        label = f"{obj.party.pk} - {name}"
        return format_html('<a href="{}">{}</a>', url, label)

    @admin.display(description="Category")
    def category(self, obj):
        # Party classification from the linked Party row:
        # Adjust this if your Party model uses something else
        # (you already use party.party_type elsewhere)
        return "NON-PARTY" if getattr(obj.party, "party_type", "") == "non_party" else "PARTY"

    # ---------- Nominated rep ----------
    @admin.display(description="Current nominated rep")
    def current_rep(self, obj):
        rep = (
            obj.nominated_reps
            .filter(date_end__isnull=True)
            .order_by("-date_start")
            .first()
        )
        if rep is None:
            rep = obj.nominated_reps.order_by("-date_start").first()

        if not rep:
            return "-"

        name = f"{rep.first_name} {rep.surname}".strip()
        url = reverse("admin:membership_nominatedrep_change", args=[rep.pk])
        return format_html('<a href="{}">{}</a>', url, name)
    
    company_charity_number_display.short_description = "Company/Charity Number"
    
    # ---------- Authorised signatory ----------
    @admin.display(description="Current authorised signatory")
    def current_signatory(self, obj):
        signatory = (
            obj.authorised_signatories
            .filter(date_end__isnull=True)
            .order_by("-date_start")
            .first()
        )
        if not signatory:
            return "-"

        name = f"{signatory.first_name} {signatory.surname}".strip()
        url = reverse("admin:membership_authorisedsignatory_change", args=[signatory.pk])
        return format_html('<a href="{}">{}</a>', url, name)


# ---------------------------------------
# ---------------- PLAN  ----------------
# ---------------------------------------
@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    class Media:
        css = {"all": ("admin/custom_admin.css",)}

    list_display = ("plan_id", "name", "class_code", "type_code", "duration", "cost", "share_capital", "active", "declaration_set")
    list_filter = ("active",)
    search_fields = ("name", "class_code", "type_code")

    @admin.display(description="Category")
    def category(self, obj):
        return "NON-MEMBER" if obj.class_code == "NM" else "MEMBER"
    

# ---------------------------------------
# ------------- ENROLMENT  --------------
# ---------------------------------------

def _pounds_decimal_to_pence(value):
    # value is Decimal pounds (e.g. 12.34) -> 1234
    if value is None:
        return 0
    return int(round(float(value) * 100))

def _int_pounds_to_pence(value):
    # plan.share_capital appears to be integer pounds -> pence
    if not value:
        return 0
    return int(value) * 100


@admin.action(description="Create payment request(s) for selected enrolments")
def create_payment_requests(modeladmin, request, queryset):
    now = timezone.now()
    due = (now + timedelta(days=28)).date()

    created_count = 0
    skipped_count = 0
    activated_free_count = 0

    for enrolment in queryset:
        type_code = (getattr(enrolment.plan, "type_code", "") or "").strip().upper()
        if type_code in {"SU", "VO"}:
            changed_fields = []
            if enrolment.status != "active":
                enrolment.status = "active"
                changed_fields.append("status")
            if not enrolment.date_start:
                enrolment.date_start = now.date()
                changed_fields.append("date_start")
            if changed_fields:
                enrolment.save(update_fields=changed_fields)
                activated_free_count += 1

            if not enrolment.membership_activated_email_sent_at:
                try:
                    to_email = enrolment.get_contact_email()
                    if to_email:
                        marketing_consent = enrolment.declarations.filter(
                            is_current=True
                        ).values_list("data_marketing_consent", flat=True).first()
                        send_membership_activated_email(
                            to_email=to_email,
                            enrolment_code=enrolment.enrolment_code or str(enrolment.enrolment_id),
                            plan_name=enrolment.plan.name if enrolment.plan_id else None,
                            marketing_consent=bool(marketing_consent),
                        )
                        enrolment.membership_activated_email_sent_at = timezone.now()
                        enrolment.save(update_fields=["membership_activated_email_sent_at"])
                except Exception as e:
                    modeladmin.message_user(
                        request,
                        f"Free enrolment {enrolment.pk}: activation email failed: {e}",
                        level=messages.ERROR,
                    )

            skipped_count += 1
            continue

        # decide which payment to create (ONE only)
        payment = None

        share_pence = enrolment.investment_amount_pence or 0

        if share_pence > 0:
            # share investment payment
            exists = enrolment.payments.filter(
                purpose="share_capital",
                status__in=["requested", "pending", "succeeded"],
            ).exists()
            if not exists:
                payment = Payment.objects.create(
                    enrolment=enrolment,
                    purpose="share_capital",
                    status="requested",
                    amount_pence=share_pence,
                    currency="GBP",
                    payment_method="online_payment",
                    requested_at=now,
                    due_date=due,
                    provider="stripe",
                )
                created_count += 1
            else:
                skipped_count += 1

        else:
            # membership fee payment (includes the £1 share automatically)
            fee_purpose = enrolment.fee_payment_purpose()
            fee_pence = 0
            if enrolment.agreed_fee is not None:
                agreed_pence = _pounds_decimal_to_pence(enrolment.agreed_fee)
                if agreed_pence > 0:
                    fee_pence = agreed_pence

            if fee_pence <= 0 and enrolment.plan and enrolment.plan.cost is not None:
                fee_pence = _pounds_decimal_to_pence(enrolment.plan.cost)

            if fee_pence <= 0:
                skipped_count += 1
                continue

            payment = Payment.objects.create(
                enrolment=enrolment,
                purpose=fee_purpose,
                status="requested",
                amount_pence=fee_pence,
                currency="GBP",
                payment_method="online_payment",
                requested_at=now,
                due_date=due,
                provider="stripe",
            )
        created_count += 1

        if payment is None:
            skipped_count += 1
            continue

        
        pid = payment.payment_id
        try:
            payment = ensure_checkout_session(payment)

            send_payment_requested_email(
                to_email=enrolment.get_contact_email(),
                enrolment_code=enrolment.enrolment_code,
                checkout_url=payment.checkout_url,
                amount_pence=payment.amount_pence,
            )

            enrolment.payment_requested_email_sent_at = timezone.now()
            enrolment.save(update_fields=["payment_requested_email_sent_at"])

            if enrolment.status in ["submitted", "approved_pending_payment"]:
                enrolment.status = "approved_pending_payment"
                enrolment.approved_at = enrolment.approved_at or now
                enrolment.approved_by = enrolment.approved_by or request.user
                enrolment.save(update_fields=["status", "approved_at", "approved_by"])

        except Exception as e:
            modeladmin.message_user(
                request,
                f"Payment {pid}: checkout/email failed: {e}",
                level=messages.ERROR,
            )

    modeladmin.message_user(
        request,
        f"Created {created_count} payment request(s). Activated {activated_free_count} free enrolment(s). Skipped {skipped_count}.",
        level=messages.SUCCESS if created_count else messages.WARNING,
    )

@admin.action(description="Mark selected payments as succeeded (manual) and activate enrolments if fully paid")
def mark_payments_succeeded_manual(modeladmin, request, queryset):
    now = timezone.now()
    updated = 0
    activated = 0

    # update payments
    for p in queryset:
        if p.status != "succeeded":
            p.status = "succeeded"
            p.paid_at = now
            p.payment_method = p.payment_method or "bank_transfer"
            p.provider = p.provider or "manual"
            p.save(update_fields=["status", "paid_at", "payment_method", "provider"])
            updated += 1

        # activate enrolment if fully paid
        if p.enrolment and p.enrolment.status != "active" and p.enrolment.is_fully_paid():
            p.enrolment.status = "active"
            p.enrolment.date_start = p.enrolment.date_start or now.date()
            p.enrolment.save(update_fields=["status", "date_start"])
            activated += 1

    modeladmin.message_user(
        request,
        f"Marked {updated} payment(s) as succeeded. Activated {activated} enrolment(s).",
        level=messages.SUCCESS
    )


@admin.register(Enrolment)
class EnrolmentAdmin(admin.ModelAdmin):
    class Media:
        css = {"all": ("admin/custom_admin.css",)}

    list_display = (
        "enrolment_id",
        "party_id_clickable",
        "plan_id_and_name",
        "enrolment_code",
        "status",
        "agreed_fee",
        "created_at",
        "date_start",
        "date_end",
    )
    list_filter = ("status", "plan",)
    search_fields = ("enrolment_code", "party__display_name", "party__postcode")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    autocomplete_fields = ("party", "plan")
    readonly_fields = (
        "enrolment_id", "created_at", "approved_by", "approved_at"
    )
    actions = [approve_enrolments, create_payment_requests, cancel_enrolments]
    fieldsets = (
        ("Enrolment Details", {"fields": (
            "enrolment_id", "party", "plan", "created_at", "approved_by", "approved_at", "enrolment_code", "enrolment_class",
            "status", "investment_amount_pence", "date_start", "date_end", "serial_number", "agreed_fee"
        )}),
    )

    @admin.display(description="Party ID - Name", ordering="party__party_id")
    def party_id_clickable(self, obj):
        return party_subtype_link(obj.party)

    @admin.display(description="Plan ID - Name", ordering="plan__plan_id")
    def plan_id_and_name(self, obj):
        if not obj.plan:
            return "-"
        url = reverse("admin:membership_plan_change", args=[obj.plan.pk])
        label = f"{obj.plan.plan_id} — {obj.plan.name}"
        return format_html('<a href="{}">{}</a>', url, label)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "payment_id", "enrolment", "donor_name", "donor_email", "purpose", "status", "amount_pence", "currency",
        "provider", "paid_at", "covers_start", "covers_end", "created_at",
    )
    list_filter = ("purpose", "status", "provider", "currency", "created_at")
    search_fields = ("payment_id", "provider_ref", "enrolment__enrolment_code", "donor_name", "donor_email")
    autocomplete_fields = ("enrolment",)
    ordering = ("-created_at",)
    actions = [mark_payments_succeeded_manual]


# ---------------------------------------
# ------------ DECLARATIONS -------------
# ---------------------------------------
@admin.register(DeclarationForm)
class DeclarationFormAdmin(admin.ModelAdmin):
    list_display = ("form_id", "declaration_set", "version", "is_active", "created_at", "ended_at")
    list_filter = ("is_active", "declaration_set")
    search_fields = ("declaration_set", "version", "content")
    ordering = ("-created_at",)
    readonly_fields = ("form_id", "created_at")

    def save_model(self, request, obj, form, change):
        now = timezone.now()

        with transaction.atomic():
            if obj.is_active:
                # Deactivate any other active form in this set
                (DeclarationForm.objects
                    .filter(declaration_set=obj.declaration_set, is_active=True, ended_at__isnull=True)
                    .exclude(pk=obj.pk)
                    .update(is_active=False, ended_at=now)
                )
                # Active form must have no ended_at
                obj.ended_at = None

            super().save_model(request, obj, form, change)


@admin.register(Declaration)
class DeclarationsAdmin(admin.ModelAdmin):
    list_display = ("declarations_id", "enrolment_link", "form", "is_current", "approved_at")
    list_filter = ("is_current", "form")
    search_fields = (
        "enrolment__enrolment_code", 
        "enrolment__party__display_name", 
        "enrolment__party__organisation__org_name",
        "enrolment__party__individual__email_address",
    )
    date_hierarchy = "approved_at"
    ordering = ("-approved_at",)
    autocomplete_fields = ("enrolment", "form")
    readonly_fields = ("declarations_id", "approved_at")
    fieldsets = (
        ("Declaration", {"fields": (
            "declarations_id", "enrolment", "form", "is_current", "approved_at",
        )}),
        ("Conflict of Interest", {"fields": (
            "conflict_of_interest", "conflict_of_interest_details", "conflict_of_interest_details_accurate_info_declaration",
        )}),
        ("Data & Consent", {"fields": (
            "data_contact_consent", "data_marketing_consent",
        )}),
        ("Voting / Terms", {"fields": (
            "commitment_accepted", "ind_voting_share_acknowledged", "ind_voting_eligibility_confirmed", 
            "org_voting_rights_terms_accepted",
        )}),
    )

    class Media:
        css = {"all": ("admin/custom_admin.css",)}

    @admin.display(description="Enrolment")
    def enrolment_link(self, obj):
        if not obj.enrolment:
            return "-"
        url = reverse("admin:membership_enrolment_change", args=[obj.enrolment.pk])
        label = f"{obj.enrolment.pk} — {obj.enrolment.enrolment_code or '-'}"
        return format_html('<a href="{}">{}</a>', url, label)

# ---------------------------------------
# --------- ORG HISTORY TABLES ----------
# ---------------------------------------
@admin.register(NominatedRep)
class NominatedRepAdmin(admin.ModelAdmin):
    list_display = ("nominated_rep_id", "organisation_clickable", "first_name", "surname", "date_start", "date_end")
    list_display_links = ("nominated_rep_id",)  
    list_filter = ("date_end",)
    search_fields = ("first_name", "surname", "email_address", "org_party__org_name")
    ordering = ("-date_start",)
    autocomplete_fields = ("org_party",)
    readonly_fields = ("nominated_rep_id",)

    class Media:
        css = {"all": ("admin/custom_admin.css",)}

    @admin.display(description="Organisation ID - Name", ordering="org_party__party__party_id")
    def organisation_clickable(self, obj):
        org = obj.org_party
        if not org:
            return "-"
        url = reverse("admin:membership_organisation_change", args=[org.pk])

        org_id = org.pk
        org_name = (org.org_name or "").strip() or "(no name)"
        label = f"{org_id} - {org_name}"

        return format_html('<a href="{}">{}</a>', url, label)

    @admin.display(description="Party", ordering="org_party__party__party_id")
    def party_clickable(self, obj):
        if not obj.org_party or not obj.org_party.party:
            return "-"
        return party_subtype_link(obj.org_party.party)
    

@admin.register(AuthorisedSignatory)
class AuthorisedSignatoryAdmin(admin.ModelAdmin):
    list_display = ("authorised_signatory_id", "organisation_clickable", "first_name", "surname", "position", "date_start", "date_end")
    list_display_links = ("authorised_signatory_id",)  
    list_filter = ("date_end",)
    search_fields = ("first_name", "surname", "position", "org_party__org_name")
    ordering = ("-date_start",)
    autocomplete_fields = ("org_party",)
    readonly_fields = ("authorised_signatory_id",)

    class Media:
        css = {"all": ("admin/custom_admin.css",)}

    @admin.display(description="Organisation ID - Name", ordering="org_party__party__party_id")
    def organisation_clickable(self, obj):
        org = obj.org_party
        if not org:
            return "-"
        url = reverse("admin:membership_organisation_change", args=[org.pk])

        org_id = org.pk
        org_name = (org.org_name or "").strip() or "(no name)"
        label = f"{org_id} - {org_name}"

        return format_html('<a href="{}">{}</a>', url, label)

    @admin.display(description="Party", ordering="org_party__party__party_id")
    def party_clickable(self, obj):
        if not obj.org_party or not obj.org_party.party:
            return "-"
        return party_subtype_link(obj.org_party.party)


def party_subtype_link(party):
    """
    Clickable link to the correct Party subtype,
    displaying: <party_id> - <display_name>
    """
    if not party:
        return "-"

    if party.party_type == "individual":
        url = reverse("admin:membership_individual_change", args=[party.pk])
    elif party.party_type == "organisation":
        url = reverse("admin:membership_organisation_change", args=[party.pk])
    else:
        url = reverse("admin:membership_party_change", args=[party.pk])

    label = f"{party.pk} - {party.display_name}"
    return format_html('<a href="{}">{}</a>', url, label)


def plan_link(plan):
    """
    Returns a clickable link to the Plan admin change page.
    """
    if not plan:
        return "-"
    url = reverse("admin:membership_plan_change", args=[plan.pk])
    return format_html('<a href="{}">{}</a>', url, plan.pk)
