from django.db import transaction
from django.utils import timezone
from django.db.models import Max
from datetime import timedelta
from membership.models import (
    Application, Party, Individual, Organisation, Enrolment, Plan,
    Declaration, NominatedRep, AuthorisedSignatory,
    Payment
)
from membership.emails import send_payment_requested_email, send_membership_activated_email
from membership.stripe_service import ensure_checkout_session
from membership.admin import (
    _normalize_submission_keys, _to_bool, _parse_date,
    _get_active_declaration_form_for_plan, _agreed_fee_for_plan,
    _plan_codes, _advisory_lock, CLASS_CODE_TO_CATEGORY,
    _pounds_decimal_to_pence
)
import os

@transaction.atomic
def process_application(*, application_id: int) -> Enrolment| None:
    """
    Creates Party + subtype + Enrolment + Declaration for ONE application.
    Returns the created enrolment.
    If already processed, return the existing enrolment (or None if you don't store link).
    """

    submission = Application.objects.select_for_update().get(pk=application_id)
    if submission.status == "processed":
        return None
    
    plan_key = (submission.plan or "").strip().upper()
    cc, tc = plan_key.split("-", 1)
    plan = Plan.objects.get(class_code=cc, type_code=tc, active=True)

    d = _normalize_submission_keys(submission.data or {})
    d.pop("cf_turnstile_response", None)

    today = timezone.now().date()
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
        if submission.logo_file:
            org.logo_file.save(os.path.basename(submission.logo_file.name), submission.logo_file, save=True )

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

    submission.status = "processed"
    submission.save(update_fields=["status"])

    return enrolment


def approve_enrolment(*, enrolment: Enrolment, approved_by):
    _advisory_lock()
    m = Enrolment.objects.select_for_update().select_related("plan").get(pk=enrolment.pk)
    if m.status != "submitted":
        return m

    class_code, type_code = _plan_codes(m.plan)
    prefix = f"{class_code}-{type_code}"

    next_serial = (
        Enrolment.objects
        .filter(enrolment_code__startswith=prefix + "-", serial_number__isnull=False)
        .aggregate(mx=Max("serial_number"))["mx"] or 0
    ) + 1

    now = timezone.now()
    m.serial_number = next_serial
    m.enrolment_code = f"{prefix}-{next_serial:04d}"
    m.approved_by = approved_by
    m.approved_at = now
    m.status = "approved_pending_payment"
    m.save(update_fields=["serial_number","enrolment_code","approved_by","approved_at","status"])

    Declaration.objects.filter(enrolment=m, is_current=True, approved_at__isnull=True).update(approved_at=now)
    return m

def request_payment(*, enrolment: Enrolment, send_email: bool = True):
    class_code = (getattr(enrolment.plan, "class_code", "") or "").strip().upper()
    type_code = (getattr(enrolment.plan, "type_code", "") or "").strip().upper()
    if type_code in {"SU", "VO"}:
        changed_fields = []
        if enrolment.status != "active":
            enrolment.status = "active"
            changed_fields.append("status")
        if not enrolment.date_start:
            enrolment.date_start = timezone.now().date()
            changed_fields.append("date_start")
        if changed_fields:
            enrolment.save(update_fields=changed_fields)

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
                        plan_code=f"{class_code}-{type_code}" if class_code else None,
                        marketing_consent=bool(marketing_consent),
                    )
                    enrolment.membership_activated_email_sent_at = timezone.now()
                    enrolment.save(update_fields=["membership_activated_email_sent_at"])
            except Exception as e:
                print("Membership activated email failed:", e)
        return None

    now = timezone.now()
    due = (now + timedelta(days=28)).date()

    def exists(purpose):
        return enrolment.payments.filter(purpose=purpose, status__in=["requested","pending","succeeded"]).exists()

    share_pence = enrolment.investment_amount_pence or 0

    if share_pence > 0:
        if exists("share_capital"):
            return None
        payment = Payment.objects.create(
            enrolment=enrolment, purpose="share_capital", status="requested",
            amount_pence=share_pence, currency="GBP", payment_method="online_payment",
            requested_at=now, due_date=due, provider="stripe",
        )
    else:
        fee_purpose = enrolment.fee_payment_purpose()
        fee_pence = 0
        if enrolment.agreed_fee is not None:
            agreed_pence = _pounds_decimal_to_pence(enrolment.agreed_fee)
            if agreed_pence > 0:
                fee_pence = agreed_pence

        if fee_pence <= 0 and enrolment.plan and enrolment.plan.cost is not None:
            fee_pence = _pounds_decimal_to_pence(enrolment.plan.cost)

        if fee_pence <= 0 or exists(fee_purpose):
            return None

        payment = Payment.objects.create(
            enrolment=enrolment, purpose=fee_purpose, status="requested",
            amount_pence=fee_pence, currency="GBP", payment_method="online_payment",
            requested_at=now, due_date=due, provider="stripe",
        )

    if send_email:
        def after_commit():
            try:
                p = ensure_checkout_session(payment)
                to_email = enrolment.get_contact_email()
                if not to_email:
                    return

                send_payment_requested_email(
                    to_email=to_email,
                    enrolment_code=enrolment.enrolment_code or str(enrolment.enrolment_id),
                    checkout_url=p.checkout_url,
                    amount_pence=p.amount_pence,
                )
                enrolment.payment_requested_email_sent_at = timezone.now()
                enrolment.save(update_fields=["payment_requested_email_sent_at"])
            except Exception as e:
                print("Payment checkout/email failed:", e)

        transaction.on_commit(after_commit)

    return payment
