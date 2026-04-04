
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='AsteriskServer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('description', models.TextField(blank=True)),
                ('server_ip', models.GenericIPAddressField()),
                ('is_active', models.BooleanField(default=True)),
                ('ari_host', models.CharField(default='127.0.0.1', max_length=200)),
                ('ari_port', models.PositiveIntegerField(default=8088)),
                ('ari_username', models.CharField(max_length=100)),
                ('ari_password', models.CharField(max_length=100)),
                ('ari_app_name', models.CharField(default='dialflow', max_length=100)),
                ('ami_host', models.CharField(default='127.0.0.1', max_length=200)),
                ('ami_port', models.PositiveIntegerField(default=5038)),
                ('ami_username', models.CharField(max_length=100)),
                ('ami_password', models.CharField(max_length=100)),
                ('connection_status', models.CharField(
                    choices=[('connected','Connected'),('disconnected','Disconnected'),
                             ('error','Error'),('unknown','Unknown')],
                    default='unknown', max_length=20,
                )),
                ('last_connected', models.DateTimeField(blank=True, null=True)),
            ],
            options={'verbose_name': 'Asterisk Server'},
        ),
        migrations.CreateModel(
            name='PjsipAor',
            fields=[
                ('id', models.CharField(max_length=40, primary_key=True, serialize=False)),
                ('max_contacts', models.IntegerField(default=1)),
                ('remove_existing', models.CharField(default='yes', max_length=10)),
                ('qualify_frequency', models.IntegerField(default=30)),
            ],
            options={'db_table': 'ps_aors', 'managed': True},
        ),
        migrations.CreateModel(
            name='PjsipAuth',
            fields=[
                ('id', models.CharField(max_length=40, primary_key=True, serialize=False)),
                ('auth_type', models.CharField(default='userpass', max_length=20)),
                ('username', models.CharField(max_length=40)),
                ('password', models.CharField(max_length=80)),
            ],
            options={'db_table': 'ps_auths', 'managed': True},
        ),
        migrations.CreateModel(
            name='PjsipEndpoint',
            fields=[
                ('id', models.CharField(max_length=40, primary_key=True, serialize=False)),
                ('transport', models.CharField(default='transport-udp', max_length=40)),
                ('aors', models.CharField(blank=True, max_length=200)),
                ('auth', models.CharField(blank=True, max_length=40)),
                ('context', models.CharField(default='agents', max_length=40)),
                ('disallow', models.CharField(default='all', max_length=200)),
                ('allow', models.CharField(default='opus,ulaw,alaw', max_length=200)),
                ('direct_media', models.CharField(default='no', max_length=10)),
                ('force_rport', models.CharField(default='yes', max_length=10)),
                ('rewrite_contact', models.CharField(default='yes', max_length=10)),
                ('rtp_symmetric', models.CharField(default='yes', max_length=10)),
                ('ice_support', models.CharField(default='yes', max_length=10)),
                ('use_avpf', models.CharField(default='yes', max_length=10)),
                ('media_encryption', models.CharField(default='dtls', max_length=20)),
                ('dtls_verify', models.CharField(default='fingerprint', max_length=20)),
                ('dtls_setup', models.CharField(default='actpass', max_length=20)),
                ('bundle', models.CharField(default='yes', max_length=10)),
                ('webrtc', models.CharField(default='yes', max_length=10)),
                ('dtmf_mode', models.CharField(default='rfc4733', max_length=20)),
                ('send_rpid', models.CharField(default='yes', max_length=10)),
            ],
            options={'db_table': 'ps_endpoints', 'managed': True},
        ),
        migrations.CreateModel(
            name='Carrier',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('description', models.TextField(blank=True)),
                ('protocol', models.CharField(
                    choices=[('pjsip','PJSIP (recommended)'),('sip','SIP (legacy)')],
                    default='pjsip', max_length=10,
                )),
                ('host', models.CharField(max_length=200)),
                ('port', models.PositiveIntegerField(default=5060)),
                ('username', models.CharField(blank=True, max_length=100)),
                ('password', models.CharField(blank=True, max_length=100)),
                ('caller_id', models.CharField(blank=True, max_length=100)),
                ('max_channels', models.PositiveIntegerField(default=30)),
                ('dial_prefix', models.CharField(blank=True, max_length=20)),
                ('is_active', models.BooleanField(default=True)),
                ('asterisk_server', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='carriers', to='telephony.asteriskserver',
                )),
            ],
            options={'verbose_name': 'Carrier', 'ordering': ['name']},
        ),
        migrations.CreateModel(
            name='Phone',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('extension', models.CharField(max_length=20, unique=True)),
                ('name', models.CharField(max_length=100)),
                ('phone_type', models.CharField(
                    choices=[('webrtc','WebRTC (browser)'),('sip','SIP Hard/Softphone')],
                    default='webrtc', max_length=10,
                )),
                ('secret', models.CharField(max_length=64)),
                ('context', models.CharField(default='agents', max_length=100)),
                ('allow_codecs', models.CharField(default='opus,ulaw,alaw', max_length=200)),
                ('is_active', models.BooleanField(default=True)),
                ('last_registered', models.DateTimeField(blank=True, null=True)),
                ('last_ip', models.GenericIPAddressField(blank=True, null=True)),
                ('asterisk_server', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='phones', to='telephony.asteriskserver',
                )),
                ('user', models.OneToOneField(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='phone', to='users.user',
                )),
            ],
            options={'verbose_name': 'Phone Extension', 'ordering': ['extension']},
        ),
    ]
