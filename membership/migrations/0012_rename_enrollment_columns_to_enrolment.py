from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0010_rename_tables_to_enrolment"),
    ]

    operations = [
        # membership_enrolment columns
        migrations.RunSQL(
            sql='ALTER TABLE "membership_enrolment" RENAME COLUMN "enrollment_code" TO "enrolment_code";',
            reverse_sql='ALTER TABLE "membership_enrolment" RENAME COLUMN "enrolment_code" TO "enrollment_code";',
        ),
        migrations.RunSQL(
            sql='ALTER TABLE "membership_enrolment" RENAME COLUMN "enrollment_class" TO "enrolment_class";',
            reverse_sql='ALTER TABLE "membership_enrolment" RENAME COLUMN "enrolment_class" TO "enrollment_class";',
        ),

        # membership_enrolmentpayment FK column
        migrations.RunSQL(
            sql='ALTER TABLE "membership_enrolmentpayment" RENAME COLUMN "enrollment_id" TO "enrolment_id";',
            reverse_sql='ALTER TABLE "membership_enrolmentpayment" RENAME COLUMN "enrolment_id" TO "enrollment_id";',
        ),
    ]
