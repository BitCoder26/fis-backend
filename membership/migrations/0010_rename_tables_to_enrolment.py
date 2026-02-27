from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0009_rename_enrollment_model_to_enrolment"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = 'membership_enrollment'
                ) THEN
                    ALTER TABLE "membership_enrollment" RENAME TO "membership_enrolment";
                END IF;
            END $$;
            """,
            reverse_sql="""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = 'membership_enrolment'
                ) THEN
                    ALTER TABLE "membership_enrolment" RENAME TO "membership_enrollment";
                END IF;
            END $$;
            """,
        ),
        migrations.RunSQL(
            sql="""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = 'membership_enrollmentpayment'
                ) THEN
                    ALTER TABLE "membership_enrollmentpayment" RENAME TO "membership_enrolmentpayment";
                END IF;
            END $$;
            """,
            reverse_sql="""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = 'membership_enrolmentpayment'
                ) THEN
                    ALTER TABLE "membership_enrolmentpayment" RENAME TO "membership_enrollmentpayment";
                END IF;
            END $$;
            """,
        ),
    ]
