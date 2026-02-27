from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0008_rename_enrollment_to_enrolment"),
    ]

    operations = [
        migrations.RunSQL(
            sql='ALTER TABLE "membership_enrollment" RENAME TO "membership_enrolment";',
            reverse_sql='ALTER TABLE "membership_enrolment" RENAME TO "membership_enrollment";',
        ),
        migrations.RunSQL(
            sql='ALTER TABLE "membership_enrollmentpayment" RENAME TO "membership_enrolmentpayment";',
            reverse_sql='ALTER TABLE "membership_enrolmentpayment" RENAME TO "membership_enrollmentpayment";',
        ),
    ]
