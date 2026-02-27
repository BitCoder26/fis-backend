from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0013_merge_20260224_1436"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.RenameModel(old_name="Enrollment", new_name="Enrolment"),
                migrations.RenameModel(old_name="EnrollmentPayment", new_name="EnrolmentPayment"),
            ],
        ),
    ]
