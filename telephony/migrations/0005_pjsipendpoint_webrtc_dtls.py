from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('telephony', '0004_pjsipaor_contact'),
    ]

    operations = [
        migrations.AddField(
            model_name='pjsipendpoint',
            name='dtls_auto_generate_cert',
            field=models.CharField(default='no', max_length=10),
        ),
        migrations.AddField(
            model_name='pjsipendpoint',
            name='rtcp_mux',
            field=models.CharField(default='no', max_length=10),
        ),
    ]
