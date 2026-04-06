# telephony/models.py
import uuid
import secrets
import string
from django.db import models
from django.conf import settings
from core.models import TimestampedModel


# ─── Asterisk Server ──────────────────────────────────────────────────────────

class AsteriskServer(TimestampedModel):
    """Single Asterisk server configuration."""

    name        = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    server_ip   = models.GenericIPAddressField()
    is_active   = models.BooleanField(default=True)

    # ARI
    ari_host     = models.CharField(max_length=200, default='127.0.0.1')
    ari_port     = models.PositiveIntegerField(default=8088)
    ari_username = models.CharField(max_length=100)
    ari_password = models.CharField(max_length=100)
    ari_app_name = models.CharField(max_length=100, default='dialflow')

    # AMI
    ami_host     = models.CharField(max_length=200, default='127.0.0.1')
    ami_port     = models.PositiveIntegerField(default=5038)
    ami_username = models.CharField(max_length=100)
    ami_password = models.CharField(max_length=100)

    # Status (updated by ARI worker)
    connection_status = models.CharField(
        max_length=20,
        choices=[('connected','Connected'),('disconnected','Disconnected'),('error','Error'),('unknown','Unknown')],
        default='unknown',
    )
    last_connected = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Asterisk Server'

    def __str__(self):
        return f'{self.name} ({self.server_ip})'

    @property
    def ari_base_url(self):
        return f'http://{self.ari_host}:{self.ari_port}/ari'

    @property
    def ari_ws_url(self):
        return f'ws://{self.ari_host}:{self.ari_port}/ari/events?app={self.ari_app_name}&api_key={self.ari_username}:{self.ari_password}'


# ─── Carrier (SIP trunk) ─────────────────────────────────────────────────────

class Carrier(TimestampedModel):
    """SIP trunk / carrier configuration."""
    PROTOCOL_SIP   = 'sip'
    PROTOCOL_PJSIP = 'pjsip'
    PROTOCOL_CHOICES = [
        (PROTOCOL_PJSIP, 'PJSIP (recommended)'),
        (PROTOCOL_SIP,   'SIP (legacy)'),
    ]

    name             = models.CharField(max_length=100, unique=True)
    description      = models.TextField(blank=True)
    asterisk_server  = models.ForeignKey(AsteriskServer, on_delete=models.CASCADE, related_name='carriers')
    protocol         = models.CharField(max_length=10, choices=PROTOCOL_CHOICES, default=PROTOCOL_PJSIP)
    host             = models.CharField(max_length=200, help_text='Carrier SIP host / IP')
    port             = models.PositiveIntegerField(default=5060)
    username         = models.CharField(max_length=100, blank=True)
    password         = models.CharField(max_length=100, blank=True)
    caller_id        = models.CharField(max_length=100, blank=True, help_text='Default outbound caller ID')
    max_channels     = models.PositiveIntegerField(default=30)
    dial_prefix      = models.CharField(max_length=20, blank=True, help_text='Prefix prepended to all dialed numbers')
    dialplan_code    = models.TextField(blank=True, help_text='Raw Asterisk dialplan code for this carrier')
    is_active        = models.BooleanField(default=True)

    class Meta:
        verbose_name = 'Carrier'
        ordering = ['name']

    def __str__(self):
        return f'{self.name} -> {self.host}'

    @property
    def endpoint_id(self):
        """Safe PJSIP endpoint id derived from carrier name."""
        import re
        return 'carrier_' + re.sub(r'[^a-zA-Z0-9_]', '_', self.name)[:32]

    def sync_to_asterisk(self):
        """
        Write PJSIP realtime rows for this SIP trunk/carrier.
        Asterisk reads ps_endpoints/ps_auths/ps_aors directly via ODBC.
        """
        ep_id = self.endpoint_id
        contact = f'sip:{self.host}:{self.port}'

        # ps_aors — points to carrier host
        PjsipAor.objects.update_or_create(
            id=ep_id,
            defaults={
                'max_contacts':      1,
                'remove_existing':   'yes',
                'qualify_frequency': 60,
            }
        )
        # Store the static contact in ps_contacts
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ps_contacts (id, uri, endpoint, expiration_time)
                VALUES (%s, %s, %s, '99999999')
                ON CONFLICT (id) DO UPDATE
                  SET uri = EXCLUDED.uri,
                      endpoint = EXCLUDED.endpoint,
                      expiration_time = EXCLUDED.expiration_time
                """,
                [f'{ep_id}@@{self.host}', contact, ep_id],
            )

        # ps_auths — outbound auth (only if credentials supplied)
        if self.username and self.password:
            PjsipAuth.objects.update_or_create(
                id=ep_id,
                defaults={
                    'auth_type': 'userpass',
                    'username':  self.username,
                    'password':  self.password,
                }
            )

        # ps_endpoints — outbound trunk, no WebRTC flags
        PjsipEndpoint.objects.update_or_create(
            id=ep_id,
            defaults={
                'transport':        'transport-udp',
                'aors':             ep_id,
                'auth':             ep_id if (self.username and self.password) else '',
                'context':          'from-carrier',
                'disallow':         'all',
                'allow':            'ulaw,alaw',
                'direct_media':     'no',
                'force_rport':      'yes',
                'rewrite_contact':  'yes',
                'rtp_symmetric':    'no',
                'ice_support':      'no',
                'use_avpf':         'no',
                'media_encryption': 'no',
                'dtls_verify':      'no',
                'dtls_setup':       'no',
                'bundle':           'no',
                'webrtc':           'no',
                'dtmf_mode':        'rfc4733',
                'send_rpid':        'yes',
            }
        )

        # Update the raw dialplan file
        Carrier.rebuild_asterisk_dialplan()

    def remove_from_asterisk(self):
        """Remove PJSIP realtime rows when carrier is deleted/deactivated."""
        ep_id = self.endpoint_id
        PjsipEndpoint.objects.filter(id=ep_id).delete()
        PjsipAuth.objects.filter(id=ep_id).delete()
        PjsipAor.objects.filter(id=ep_id).delete()
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("DELETE FROM ps_contacts WHERE endpoint = %s", [ep_id])

        # Update the raw dialplan file
        Carrier.rebuild_asterisk_dialplan()

    @classmethod
    def rebuild_asterisk_dialplan(cls):
        """
        Rebuilds the carriers_dialplan.conf file and reloads the Asterisk dialplan.
        """
        import os
        import subprocess
        
        file_path = '/home/easyian/dialflow/asterisk/carriers_dialplan.conf'
        carriers = cls.objects.filter(is_active=True).exclude(dialplan_code='')
        
        with open(file_path, 'w') as f:
            f.write("; Auto-generated by DialFlow Pro\\n")
            f.write("; Do not edit manually. Edit the Carrier 'dialplan_code' via Web UI.\\n\\n")
            for carrier in carriers:
                f.write(f"; Carrier: {carrier.name}\\n")
                f.write(f"{carrier.dialplan_code}\\n\\n")
                
        # Reload dialplan using sudo (requires sudoers configuration for user)
        try:
            subprocess.run(["sudo", "asterisk", "-rx", "dialplan reload"], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            # Handle failure silently but let logs record it if logging was configured here
            pass


# ─── PJSIP Realtime Tables (Asterisk reads these directly) ───────────────────

class PjsipEndpoint(models.Model):
    """Maps to ps_endpoints table — Asterisk reads this directly via ODBC."""
    id                    = models.CharField(max_length=40, primary_key=True)
    transport             = models.CharField(max_length=40, default='transport-udp')
    aors                  = models.CharField(max_length=200, blank=True)
    auth                  = models.CharField(max_length=40, blank=True)
    context               = models.CharField(max_length=40, default='agents')
    disallow              = models.CharField(max_length=200, default='all')
    allow                 = models.CharField(max_length=200, default='opus,ulaw,alaw')
    direct_media          = models.CharField(max_length=10, default='no')
    force_rport           = models.CharField(max_length=10, default='yes')
    rewrite_contact       = models.CharField(max_length=10, default='yes')
    rtp_symmetric         = models.CharField(max_length=10, default='yes')
    ice_support           = models.CharField(max_length=10, default='yes')  # WebRTC
    use_avpf              = models.CharField(max_length=10, default='yes')  # WebRTC
    media_encryption      = models.CharField(max_length=20, default='dtls') # WebRTC
    dtls_verify           = models.CharField(max_length=20, default='fingerprint')
    dtls_setup            = models.CharField(max_length=20, default='actpass')
    bundle               = models.CharField(max_length=10, default='yes')
    webrtc               = models.CharField(max_length=10, default='yes')
    dtmf_mode             = models.CharField(max_length=20, default='rfc4733')
    send_rpid             = models.CharField(max_length=10, default='yes')

    class Meta:
        db_table = 'ps_endpoints'
        managed  = True

    def __str__(self):
        return self.id


class PjsipAuth(models.Model):
    id            = models.CharField(max_length=40, primary_key=True)
    auth_type     = models.CharField(max_length=20, default='userpass')
    username      = models.CharField(max_length=40)
    password      = models.CharField(max_length=80)

    class Meta:
        db_table = 'ps_auths'
        managed  = True

    def __str__(self):
        return self.id


class PjsipAor(models.Model):
    id                 = models.CharField(max_length=40, primary_key=True)
    max_contacts       = models.IntegerField(default=1)
    remove_existing    = models.CharField(max_length=10, default='yes')
    qualify_frequency  = models.IntegerField(default=30)

    class Meta:
        db_table = 'ps_aors'
        managed  = True

    def __str__(self):
        return self.id


# ─── Phone / Extension ───────────────────────────────────────────────────────

def _generate_secret(length=16):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


class Phone(TimestampedModel):
    """
    Agent softphone extension.

    On save → writes to PjsipEndpoint / PjsipAuth / PjsipAor tables so
    Asterisk picks up the extension immediately via ODBC realtime — no
    `asterisk -rx "pjsip reload"` needed.
    """
    PHONE_WEBRTC = 'webrtc'
    PHONE_SIP    = 'sip'
    PHONE_TYPES  = [(PHONE_WEBRTC, 'WebRTC (browser)'), (PHONE_SIP, 'SIP Hard/Softphone')]

    extension        = models.CharField(max_length=20, unique=True)
    name             = models.CharField(max_length=100)
    phone_type       = models.CharField(max_length=10, choices=PHONE_TYPES, default=PHONE_WEBRTC)
    user             = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='phone',
    )
    asterisk_server  = models.ForeignKey(AsteriskServer, on_delete=models.CASCADE, related_name='phones')
    secret           = models.CharField(max_length=64, default=_generate_secret, help_text='SIP password')
    context          = models.CharField(max_length=100, default='agents')
    allow_codecs     = models.CharField(max_length=200, default='opus,ulaw,alaw')
    is_active        = models.BooleanField(default=True)
    last_registered  = models.DateTimeField(null=True, blank=True)
    last_ip          = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        verbose_name = 'Phone Extension'
        ordering = ['extension']

    def __str__(self):
        return f'{self.extension} — {self.name}'

    def get_sip_uri(self):
        domain = self.asterisk_server.server_ip
        return f'sip:{self.extension}@{domain}'

    def sync_to_asterisk(self):
        """
        Write PJSIP realtime rows. Called from post_save signal.
        Asterisk reads ps_endpoints/ps_auths/ps_aors directly from DB.
        """
        is_webrtc = (self.phone_type == self.PHONE_WEBRTC)

        # ps_endpoints
        PjsipEndpoint.objects.update_or_create(
            id=self.extension,
            defaults={
                'transport':        'transport-wss' if is_webrtc else 'transport-udp',
                'aors':             self.extension,
                'auth':             self.extension,
                'context':          self.context,
                'disallow':         'all',
                'allow':            self.allow_codecs,
                'direct_media':     'no',
                'force_rport':      'yes',
                'rewrite_contact':  'yes',
                'rtp_symmetric':    'yes',
                'ice_support':      'yes' if is_webrtc else 'no',
                'use_avpf':         'yes' if is_webrtc else 'no',
                'media_encryption': 'dtls' if is_webrtc else 'no',
                'dtls_verify':      'fingerprint' if is_webrtc else 'no',
                'dtls_setup':       'actpass' if is_webrtc else 'no',
                'bundle':           'yes' if is_webrtc else 'no',
                'webrtc':           'yes' if is_webrtc else 'no',
                'dtmf_mode':        'rfc4733',
                'send_rpid':        'yes',
            }
        )

        # ps_auths
        PjsipAuth.objects.update_or_create(
            id=self.extension,
            defaults={
                'auth_type': 'userpass',
                'username':  self.extension,
                'password':  self.secret,
            }
        )

        # ps_aors
        PjsipAor.objects.update_or_create(
            id=self.extension,
            defaults={
                'max_contacts':      1,
                'remove_existing':   'yes',
                'qualify_frequency': 30,
            }
        )

    def remove_from_asterisk(self):
        """Remove PJSIP realtime rows (called on delete)."""
        PjsipEndpoint.objects.filter(id=self.extension).delete()
        PjsipAuth.objects.filter(id=self.extension).delete()
        PjsipAor.objects.filter(id=self.extension).delete()
