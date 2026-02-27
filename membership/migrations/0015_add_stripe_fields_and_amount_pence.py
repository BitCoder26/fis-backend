from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0014_sync_state_enrolment_renames"),  # or whatever your latest is
    ]

    operations = [
        # Add amount_pence (int) - nullable first to avoid defaults
        migrations.RunSQL(
            sql='ALTER TABLE "membership_enrolmentpayment" ADD COLUMN IF NOT EXISTS "amount_pence" integer;',
            reverse_sql='ALTER TABLE "membership_enrolmentpayment" DROP COLUMN IF EXISTS "amount_pence";',
        ),

        # Add Stripe columns
        migrations.RunSQL(
            sql='ALTER TABLE "membership_enrolmentpayment" ADD COLUMN IF NOT EXISTS "stripe_checkout_session_id" varchar(255);',
            reverse_sql='ALTER TABLE "membership_enrolmentpayment" DROP COLUMN IF EXISTS "stripe_checkout_session_id";',
        ),
        migrations.RunSQL(
            sql='ALTER TABLE "membership_enrolmentpayment" ADD COLUMN IF NOT EXISTS "stripe_payment_intent_id" varchar(255);',
            reverse_sql='ALTER TABLE "membership_enrolmentpayment" DROP COLUMN IF EXISTS "stripe_payment_intent_id";',
        ),

        # Indexes (optional but good)
        migrations.RunSQL(
            sql='CREATE INDEX IF NOT EXISTS "membership_enrolmentpayment_stripe_checkout_session_id_idx" ON "membership_enrolmentpayment" ("stripe_checkout_session_id");',
            reverse_sql='DROP INDEX IF EXISTS "membership_enrolmentpayment_stripe_checkout_session_id_idx";',
        ),
        migrations.RunSQL(
            sql='CREATE INDEX IF NOT EXISTS "membership_enrolmentpayment_stripe_payment_intent_id_idx" ON "membership_enrolmentpayment" ("stripe_payment_intent_id");',
            reverse_sql='DROP INDEX IF EXISTS "membership_enrolmentpayment_stripe_payment_intent_id_idx";',
        ),
    ]
