from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('telephony', '0003_carrier_dialplan_code'),
    ]

    operations = [
        migrations.AddField(
            model_name='pjsipaor',
            name='contact',
            field=models.CharField(blank=True, max_length=200),
        ),
    ]
