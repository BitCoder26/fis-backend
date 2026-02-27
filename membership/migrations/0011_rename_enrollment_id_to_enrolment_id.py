from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0010_rename_tables_to_enrolment"),
    ]

    operations = [
        migrations.RunSQL(
            sql='ALTER TABLE "membership_enrolment" RENAME COLUMN "enrollment_id" TO "enrolment_id";',
            reverse_sql='ALTER TABLE "membership_enrolment" RENAME COLUMN "enrolment_id" TO "enrollment_id";',
        ),
    ]
