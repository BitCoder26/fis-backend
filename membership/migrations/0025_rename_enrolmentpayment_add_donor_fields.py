from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("membership", "0024_alter_enrolmentpayment_checkout_url"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="EnrolmentPayment",
            new_name="Payment",
        ),
        migrations.AddField(
            model_name="payment",
            name="donor_email",
            field=models.EmailField(blank=True, db_index=True, max_length=254, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="donor_name",
            field=models.TextField(blank=True, null=True),
        ),
    ]
