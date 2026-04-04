from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Lead',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('first_name', models.CharField(max_length=100)),
                ('last_name', models.CharField(blank=True, max_length=100)),
                ('email', models.EmailField(blank=True)),
                ('company', models.CharField(blank=True, max_length=200)),
                ('primary_phone', models.CharField(db_index=True, max_length=30)),
                ('alt_phone_1', models.CharField(blank=True, max_length=30)),
                ('alt_phone_2', models.CharField(blank=True, max_length=30)),
                ('address', models.TextField(blank=True)),
                ('city', models.CharField(blank=True, max_length=100)),
                ('state', models.CharField(blank=True, max_length=100)),
                ('zip_code', models.CharField(blank=True, max_length=20)),
                ('country', models.CharField(blank=True, default='IN', max_length=100)),
                ('timezone', models.CharField(blank=True, default='Asia/Kolkata', max_length=50)),
                ('is_active', models.BooleanField(default=True)),
                ('do_not_call', models.BooleanField(default=False)),
                ('priority', models.SmallIntegerField(default=5)),
                ('source', models.CharField(blank=True, max_length=100)),
                ('external_id', models.CharField(blank=True, db_index=True, max_length=100)),
                ('custom_fields', models.JSONField(blank=True, default=dict)),
            ],
            options={'verbose_name': 'Lead', 'ordering': ['-priority', 'created_at']},
        ),
        migrations.AddIndex(model_name='lead', index=models.Index(fields=['primary_phone'], name='lead_phone_idx')),
        migrations.AddIndex(model_name='lead', index=models.Index(fields=['is_active', 'do_not_call'], name='lead_active_idx')),
    ]
