from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("campaigns", "0002_rename_dnc_phone_idx_campaigns_d_phone_n_517050_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="cps_limit",
            field=models.PositiveSmallIntegerField(
                default=10,
                help_text="Maximum call originations per second (0 = unlimited)",
            ),
        ),
    ]
