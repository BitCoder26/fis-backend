import json
import requests
from django.db import transaction, IntegrityError
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.conf import settings
from datetime import date
from .emails import send_application_submitted_email


from .models import (
    Party,
    Individual,
    Organisation,
    Enrolment,
    Plan,
    DeclarationForm,
    Declaration,
    NominatedRep,
    AuthorisedSignatory,
)


# HELPER METHODS
def _parse_date(v):
    if not v:
        return None
    if isinstance(v, date):
        return v
    # expect YYYY-MM-DD
    return date.fromisoformat(v)


def _get_active_form_for_plan(plan: Plan):
    if not plan or not plan.declaration_set:
        return None
    return (
        DeclarationForm.objects
        .filter(declaration_set=plan.declaration_set, is_active=True)
        .order_by("-created_at")
        .first()
    )

@csrf_exempt
@require_POST
def submit_application(request):
    """
    Expects JSON. Minimal payload for MVP:
    {
      "plan_id": 4,
      "member_type": "organisation" | "individual",
      "member": {"address": "...", "postcode": "...", "display_name": "..."},
      "individual": {...} OR "organisation": {...},
      "declarations": {... booleans/text ...},
      "nominated_rep": {... optional ...},
      "authorised_signatory": {... optional ...}
    }
    """
    print(">>> submit_application CALLED")
    
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    
    # added
    api_key = request.headers.get("X-FIS-KEY")
    if api_key != settings.SUBMIT_API_KEY:
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=401)
    
    # added 
    # ---- Cloudflare Turnstile verification ----
    turnstile_token = request.headers.get("CF-Turnstile-Token")

    if not turnstile_token:
        return JsonResponse({"ok": False, "error": "Missing Turnstile token"}, status=400)

    verify = requests.post(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        data={
            "secret": settings.TURNSTILE_SECRET_KEY,
            "response": turnstile_token,
            "remoteip": request.META.get("REMOTE_ADDR"),
        },
    )

    result = verify.json()

    if not result.get("success"):
        return JsonResponse({"ok": False, "error": "Turnstile verification failed"}, status=403)
    # -----------------------------------------

    plan_id = payload.get("plan_id")
    member_type = payload.get("member_type")

    if not plan_id:
        return JsonResponse({"ok": False, "error": "plan_id is required"}, status=400)
    if member_type not in {"individual", "organisation"}:
        return JsonResponse({"ok": False, "error": "member_type must be 'individual' or 'organisation'"}, status=400)

    try:
        plan = Plan.objects.get(pk=plan_id)
    except Plan.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Invalid plan_id"}, status=400)

    active_form = _get_active_form_for_plan(plan)
    if not active_form:
        return JsonResponse({"ok": False, "error": f"No active declaration form for declaration_set='{plan.declaration_set}'"}, status=400)

    member_data = payload.get("member") or {}
    subtype_data = payload.get("individual") if member_type == "individual" else payload.get("organisation")
    subtype_data = subtype_data or {}
    decl_data = payload.get("declarations") or {}

    to_email = ""
    if member_type == "individual":
        to_email = (subtype_data.get("email_address") or "").strip()
    else:
        rep = payload.get("nominated_rep") or payload.get("primary_contact") or {}
        to_email = (rep.get("email_address") or "").strip()

    # ---- Minimal validations ----
    postcode = (member_data.get("postcode") or "").strip()
    address = (member_data.get("address") or "").strip()
    display_name = (member_data.get("display_name") or "").strip()

    try:
        with transaction.atomic():
            # 1) Member
            member = Party.objects.create(
                member_type=member_type,
                display_name=display_name or None,
                address=address or None,
                postcode=postcode or None,
            )

            # 2) Subtype
            if member_type == "individual":
                Individual.objects.create(
                    member=member,
                    first_name=subtype_data.get("first_name"),
                    last_name=subtype_data.get("last_name"),
                    email_address=subtype_data.get("email_address"),
                    phone_number=subtype_data.get("phone_number"),
                    preferred_contact_method=subtype_data.get("preferred_contact_method"),

                    # --- demographics ---
                    age_range=subtype_data.get("age_range"),
                    ethnic_background=subtype_data.get("ethnic_background"),
                    ethnic_background_other=subtype_data.get("ethnic_background_other"),
                    disability_health_condition=subtype_data.get("disability_health_condition"),

                    # --- motivations / application text ---
                    how_did_you_hear_about_fis=subtype_data.get("how_did_you_hear_about_fis"),
                    why_join_fis=subtype_data.get("why_join_fis"),

                    # --- signature ---
                    digital_signature=subtype_data.get("digital_signature"),
                    date_signed=_parse_date(subtype_data.get("date_signed")),

                    # --- volunteer-only fields ---
                    preferred_pronouns=subtype_data.get("preferred_pronouns"),
                    volunteer_occupation_current_role=subtype_data.get("volunteer_occupation_current_role"),
                    volunteer_areas_of_expertise=subtype_data.get("volunteer_areas_of_expertise"),
                    volunteer_areas_of_expertise_other=subtype_data.get("volunteer_areas_of_expertise_other"),
                    volunteer_why_volunteer=subtype_data.get("volunteer_why_volunteer"),
                    volunteer_time_availability=subtype_data.get("volunteer_time_availability"),
                )
            else:
                Organisation.objects.create(
                    member=member,
                    org_name=subtype_data.get("org_name"),
                    website_social=subtype_data.get("website_social"),
                    org_type=subtype_data.get("org_type"),
                    org_type_other=subtype_data.get("org_type_other"),
                    company_charity_number=subtype_data.get("company_charity_number"),
                    
                    # --- org profile ---
                    mission_purpose=subtype_data.get("mission_purpose"),
                    service_areas=subtype_data.get("service_areas"),
                    visibility_preference=subtype_data.get("visibility_preference"),
                    logo_file=subtype_data.get("logo_file"),

                    # --- alignment & collaboration ---
                    alignment_with_fis_mission=subtype_data.get("alignment_with_fis_mission"),
                    collaboration_interests=subtype_data.get("collaboration_interests"),
                    collaboration_interests_other=subtype_data.get("collaboration_interests_other"),

                    # --- impact narrative ---
                    org_activity_beneficiaries_good_food_approach=subtype_data.get(
                        "org_activity_beneficiaries_good_food_approach"
                    ),
                    healthier_fairer_food_contribution=subtype_data.get(
                        "healthier_fairer_food_contribution"
                    ),
                )

            # 3) Membership
            membership = Enrolment.objects.create(
                member=member,
                plan=plan,
                status="pending",
                annual_contribution=payload.get("annual_contribution"),
                payment_method=payload.get("payment_method"),
            )

            # 4) Declarations row linked to active form
            declarations = Declaration.objects.create(
                membership=membership,
                form=active_form,
                is_current=True,
                # approved_at=None,
                conflict_of_interest=bool(decl_data.get("conflict_of_interest", False)),
                conflict_of_interest_details=decl_data.get("conflict_of_interest_details"),
                conflict_of_interest_details_accurate_info_declaration=bool(decl_data.get("conflict_of_interest_details_accurate_info_declaration", False)),
                data_contact_consent=bool(decl_data.get("data_contact_consent", False)),
                data_withdrawal_acknowledgement=bool(decl_data.get("data_withdrawal_acknowledgement", False)),
                ind_declaration_of_support=bool(decl_data.get("ind_declaration_of_support", False)),
                ind_voting_share_acknowledged=bool(decl_data.get("ind_voting_share_acknowledged", False)),
                ind_voting_eligibility_confirmed=bool(decl_data.get("ind_voting_eligibility_confirmed", False)),
                ind_volunteer_declaration_accepted=bool(decl_data.get("ind_volunteer_declaration_accepted", False)),
                org_commitment_agreed=bool(decl_data.get("org_commitment_agreed", False)),
                org_voting_rights_terms_accepted=bool(decl_data.get("org_voting_rights_terms_accepted", False)),
            )

            # 5) Optional: nominated rep / signatory history (for org types)
            if member_type == "organisation":
                rep = payload.get("nominated_rep") or payload.get("primary_contact")
                if rep:
                    NominatedRep.objects.create(
                        org_member=member.organisation,
                        first_name=rep.get("first_name"),
                        surname=rep.get("surname"),
                        position_role=rep.get("position_role"),
                        email_address=rep.get("email_address"),
                        phone_number=rep.get("phone_number"),
                        preferred_contact_method=rep.get("preferred_contact_method"),
                        authorisation_confirmation=rep.get("authorisation_confirmation"),
                        date_start=_parse_date(rep.get("date_start")) or timezone.now().date(),
                        date_end=None,
                    )

                sig = payload.get("authorised_signatory")
                if sig:
                    AuthorisedSignatory.objects.create(
                        org_member=member.organisation,
                        first_name=sig.get("first_name"),
                        surname=sig.get("surname"),
                        position=sig.get("position"),
                        digital_signature=sig.get("digital_signature"),
                        date_signed=_parse_date(sig.get("date_signed")),
                        date_start=_parse_date(sig.get("date_start")) or timezone.now().date(),
                        date_end=None,
                    )
                
        try: 
            if to_email:
                send_application_submitted_email(
                    to_email=to_email,
                    plan_name=getattr(plan, "name", None) or str(plan),
                    enrolment_code=membership.enrolment_code,
                    plan_code=f"{plan.class_code}-{plan.type_code}",
                )
        except Exception as e:
            print("Email sent failed:", e)

        return JsonResponse({
            "ok": True,
            "member_id": member.pk,
            "membership_id": membership.pk,
            "declarations_id": declarations.pk,
            "form_id": active_form.pk,
        }, status=201)
        
    except IntegrityError as e:
        return JsonResponse({
            "ok": False,
            "error": "IntegrityError",
            "details": str(e)
        }, status=400)

    except Exception as e:
        return JsonResponse({
            "ok": False,
            "error": "ServerError",
            "details": str(e)
        }, status=500)
        
