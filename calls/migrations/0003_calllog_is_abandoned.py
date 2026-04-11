from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("calls", "0002_rename_call_campaign_idx_calls_calll_campaig_f6e270_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="calllog",
            name="is_abandoned",
            field=models.BooleanField(
                default=False,
                db_index=True,
                help_text="Customer answered but no agent was bridged (dropped call).",
            ),
        ),
    ]
