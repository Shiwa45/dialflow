# campaigns/ivr.py
"""
IVR System + Sarvam AI Calling Integration
==========================================

Supports:
1. Multi-level IVR menus with DTMF key handling
2. AI calling via Sarvam AI (realistic Hindi voice)
3. Text-to-speech for greetings and prompts
4. Call recording and transcription
5. Transfer to live agent based on AI conversation outcome

Sarvam AI API:
  - TTS: POST /text-to-speech → WAV audio
  - STT: POST /speech-to-text → transcript
  - LLM: POST /chat/completions → conversation response

Architecture:
  1. Campaign has an IVR flow (IVRFlow model)
  2. IVR flow has nodes (IVRNode model) — greeting, menu, prompt, transfer, hangup
  3. When a call is answered, ARI hands off to IVR handler
  4. IVR handler plays prompts and collects DTMF/speech
  5. AI mode: Sarvam AI handles full conversation, decides to transfer or hang up
"""
import logging
import json
import time
import threading
from typing import Optional, Dict, Any
from django.conf import settings
from django.db import models
from django.utils import timezone

logger = logging.getLogger('dialflow.ivr')


# ── IVR Models ───────────────────────────────────────────────────────────────

class IVRFlow(models.Model):
    """An IVR call flow assigned to a campaign."""
    name        = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    is_active   = models.BooleanField(default=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    # AI settings
    ai_enabled    = models.BooleanField(default=False)
    ai_persona    = models.TextField(
        blank=True,
        default="You are a friendly customer service agent calling on behalf of the company. Be concise and professional.",
    )
    ai_language   = models.CharField(max_length=10, default='hi-IN')  # Sarvam: hi-IN
    ai_voice      = models.CharField(max_length=50, default='meera')  # Sarvam voice

    # Transfer settings
    transfer_on_interest = models.BooleanField(default=True)
    transfer_extension   = models.CharField(max_length=20, blank=True)
    max_ai_turns         = models.PositiveIntegerField(default=6)

    class Meta:
        verbose_name = 'IVR Flow'

    def __str__(self):
        return self.name

    def get_start_node(self):
        return self.nodes.filter(node_type='greeting').first() or self.nodes.order_by('order').first()


class IVRNode(models.Model):
    """A single node in an IVR flow (greeting, menu, prompt, transfer, hangup)."""
    NODE_TYPES = [
        ('greeting', 'Greeting message'),
        ('menu',     'DTMF Menu'),
        ('prompt',   'Collect input'),
        ('ai',       'AI Conversation'),
        ('transfer', 'Transfer to agent'),
        ('hangup',   'Hang up'),
        ('sms',      'Send SMS'),
    ]

    flow       = models.ForeignKey(IVRFlow, on_delete=models.CASCADE, related_name='nodes')
    node_type  = models.CharField(max_length=20, choices=NODE_TYPES, default='greeting')
    name       = models.CharField(max_length=100)
    order      = models.PositiveIntegerField(default=0)

    # Text/speech content
    message_text  = models.TextField(blank=True, help_text='Message to speak (TTS)')
    message_audio = models.CharField(max_length=500, blank=True, help_text='Pre-recorded audio file path')
    language      = models.CharField(max_length=10, default='hi-IN')

    # DTMF menu options (JSON: {"1": node_id, "2": node_id, ...})
    dtmf_options  = models.JSONField(default=dict, blank=True)

    # Next node (when no branching)
    next_node = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    no_input_next = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
        help_text='Node to go to if no input received'
    )

    # Collect settings
    collect_digits   = models.PositiveIntegerField(default=1, help_text='Max digits to collect')
    collect_timeout  = models.PositiveIntegerField(default=5, help_text='Seconds to wait for input')

    # Transfer settings
    transfer_to      = models.CharField(max_length=100, blank=True, help_text='Extension or queue to transfer to')

    class Meta:
        verbose_name = 'IVR Node'
        ordering     = ['flow', 'order']

    def __str__(self):
        return f'{self.flow.name} → {self.name}'


# ── Sarvam AI Client ─────────────────────────────────────────────────────────

class SarvamAIClient:
    """
    Client for Sarvam AI APIs.
    Docs: https://docs.sarvam.ai/
    Supports: TTS (meera/arjun voices), STT, chat completions.
    """

    BASE_URL = 'https://api.sarvam.ai'

    def __init__(self):
        cfg = getattr(settings, 'SARVAM_AI', {})
        self.api_key  = cfg.get('API_KEY', '')
        self.enabled  = bool(self.api_key)
        if not self.enabled:
            logger.warning('Sarvam AI API key not configured — AI calling disabled')

    def text_to_speech(
        self,
        text:     str,
        language: str = 'hi-IN',
        voice:    str = 'meera',
        speed:    float = 1.0,
    ) -> Optional[bytes]:
        """
        Convert text to speech using Sarvam AI.
        Returns WAV audio bytes or None on failure.

        Voices: meera (female), arjun (male), anushka, kalpana, etc.
        Languages: hi-IN, en-IN, ta-IN, te-IN, kn-IN, mr-IN, bn-IN, gu-IN, ml-IN
        """
        if not self.enabled:
            return None
        import requests
        try:
            resp = requests.post(
                f'{self.BASE_URL}/text-to-speech',
                headers={
                    'API-Subscription-Key': self.api_key,
                    'Content-Type': 'application/json',
                },
                json={
                    'inputs':          [text],
                    'target_language_code': language,
                    'speaker':         voice,
                    'pitch':           0,
                    'pace':            speed,
                    'loudness':        1.5,
                    'speech_sample_rate': 8000,   # 8kHz for telephony
                    'enable_preprocessing': True,
                    'model':           'bulbul:v1',  # Sarvam's best voice model
                },
                timeout=10,
            )
            resp.raise_for_status()
            audio_base64 = resp.json().get('audios', [''])[0]
            if audio_base64:
                import base64
                return base64.b64decode(audio_base64)
        except Exception as exc:
            logger.error(f'Sarvam TTS error: {exc}')
        return None

    def speech_to_text(
        self,
        audio_bytes: bytes,
        language:    str = 'hi-IN',
    ) -> Optional[str]:
        """
        Transcribe speech using Sarvam AI STT.
        Returns transcript string or None.
        """
        if not self.enabled:
            return None
        import requests
        try:
            import base64
            audio_b64 = base64.b64encode(audio_bytes).decode()
            resp = requests.post(
                f'{self.BASE_URL}/speech-to-text',
                headers={
                    'API-Subscription-Key': self.api_key,
                    'Content-Type': 'application/json',
                },
                json={
                    'audio_base64': audio_b64,
                    'source_language': language,
                    'model': 'saarika:v2',
                },
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json().get('transcript', '')
        except Exception as exc:
            logger.error(f'Sarvam STT error: {exc}')
        return None

    def chat(
        self,
        messages:  list,
        language:  str = 'hi-IN',
        max_tokens: int = 200,
    ) -> Optional[str]:
        """
        Generate AI response using Sarvam chat completions.
        Returns response text or None.
        """
        if not self.enabled:
            return None
        import requests
        try:
            resp = requests.post(
                f'{self.BASE_URL}/chat/completions',
                headers={
                    'API-Subscription-Key': self.api_key,
                    'Content-Type': 'application/json',
                },
                json={
                    'model':       'saaras:v1',
                    'messages':    messages,
                    'max_tokens':  max_tokens,
                    'temperature': 0.7,
                },
                timeout=15,
            )
            resp.raise_for_status()
            choices = resp.json().get('choices', [])
            if choices:
                return choices[0].get('message', {}).get('content', '')
        except Exception as exc:
            logger.error(f'Sarvam Chat error: {exc}')
        return None


# ── IVR Session Handler ──────────────────────────────────────────────────────

class IVRSession:
    """
    Manages a single IVR call session.
    Called by ARI worker when a customer call is answered.
    """

    def __init__(self, channel_id: str, flow: IVRFlow, lead_data: dict):
        self.channel_id  = channel_id
        self.flow        = flow
        self.lead        = lead_data
        self.current_node = flow.get_start_node()
        self.ai_client   = SarvamAIClient()
        self.conversation = []   # chat history for AI mode
        self.turn_count  = 0
        self.transferred = False
        self.completed   = False

    def start(self, ari_client):
        """Begin IVR session — run in a thread from ARI worker."""
        logger.info(f'IVR session started: channel={self.channel_id} flow={self.flow.name}')
        try:
            self._run(ari_client)
        except Exception as exc:
            logger.error(f'IVR session error: {exc}')
            self._hangup(ari_client)

    def _run(self, ari_client):
        """Main IVR loop."""
        while self.current_node and not self.completed and not self.transferred:
            node = self.current_node
            logger.debug(f'IVR: channel={self.channel_id} node={node.name} type={node.node_type}')

            if node.node_type == 'greeting':
                self._play_message(ari_client, node)
                self.current_node = node.next_node

            elif node.node_type == 'menu':
                dtmf = self._play_and_collect(ari_client, node)
                next_id = node.dtmf_options.get(dtmf)
                if next_id:
                    try:
                        self.current_node = IVRNode.objects.get(id=next_id)
                    except IVRNode.DoesNotExist:
                        self.current_node = node.no_input_next
                else:
                    self.current_node = node.no_input_next

            elif node.node_type == 'ai':
                self._run_ai_conversation(ari_client, node)

            elif node.node_type == 'transfer':
                self._transfer(ari_client, node)

            elif node.node_type == 'hangup':
                self._hangup(ari_client)
                return

            else:
                self.current_node = node.next_node

    def _play_message(self, ari_client, node: IVRNode):
        """Play TTS or pre-recorded audio for a node."""
        if node.message_audio:
            self._play_audio_file(ari_client, node.message_audio)
        elif node.message_text:
            audio = self.ai_client.text_to_speech(
                node.message_text,
                language=node.language or self.flow.ai_language,
                voice=self.flow.ai_voice,
            )
            if audio:
                self._play_audio_bytes(ari_client, audio)
            else:
                # Fallback: use Asterisk's say application
                self._ari_say(ari_client, node.message_text)

    def _play_and_collect(self, ari_client, node: IVRNode) -> str:
        """Play a message and collect DTMF input. Returns digit string."""
        self._play_message(ari_client, node)
        # Wait for DTMF (simplified — real implementation uses ARI events)
        time.sleep(node.collect_timeout)
        return ''  # In real ARI, collected from events

    def _run_ai_conversation(self, ari_client, node: IVRNode):
        """Run AI conversation loop using Sarvam AI."""
        if not self.ai_client.enabled:
            logger.warning('AI calling disabled — Sarvam API key not set')
            self.current_node = node.next_node
            return

        # Build system prompt
        lead_name = f"{self.lead.get('first_name','')} {self.lead.get('last_name','')}".strip()
        system_prompt = (
            f"{self.flow.ai_persona}\n\n"
            f"You are calling {lead_name or 'a customer'} "
            f"(phone: {self.lead.get('phone','')}).\n"
            f"Speak in {self.flow.ai_language} language.\n"
            f"If the customer is interested, say 'TRANSFER_TO_AGENT' to transfer them.\n"
            f"If the customer is not interested or asks to be removed, say 'HANGUP'.\n"
            f"Keep responses SHORT — max 2 sentences for phone calls.\n"
            f"Be natural and conversational."
        )

        self.conversation = [{'role': 'system', 'content': system_prompt}]

        # Opening greeting
        opening = self.ai_client.chat(
            self.conversation + [{'role': 'user', 'content': '[START]'}],
            language=self.flow.ai_language,
        )
        if opening:
            audio = self.ai_client.text_to_speech(
                opening, language=self.flow.ai_language, voice=self.flow.ai_voice
            )
            if audio:
                self._play_audio_bytes(ari_client, audio)
            self.conversation.append({'role': 'assistant', 'content': opening})

        # Conversation loop
        while self.turn_count < self.flow.max_ai_turns and not self.completed:
            self.turn_count += 1

            # Record customer speech (5 seconds)
            customer_audio = self._record_audio(ari_client, duration=5)
            if not customer_audio:
                time.sleep(1)
                continue

            # Transcribe
            transcript = self.ai_client.speech_to_text(
                customer_audio, language=self.flow.ai_language
            ) or '[inaudible]'
            logger.info(f'AI IVR: customer said: {transcript}')
            self.conversation.append({'role': 'user', 'content': transcript})

            # Generate AI response
            ai_response = self.ai_client.chat(
                self.conversation, language=self.flow.ai_language
            )
            if not ai_response:
                continue

            self.conversation.append({'role': 'assistant', 'content': ai_response})
            logger.info(f'AI IVR: agent response: {ai_response}')

            # Check for control signals
            if 'TRANSFER_TO_AGENT' in ai_response:
                self._transfer(ari_client, node)
                return
            elif 'HANGUP' in ai_response:
                self._hangup(ari_client)
                return

            # Play response
            clean_response = ai_response.replace('TRANSFER_TO_AGENT', '').replace('HANGUP', '').strip()
            if clean_response:
                audio = self.ai_client.text_to_speech(
                    clean_response, language=self.flow.ai_language, voice=self.flow.ai_voice
                )
                if audio:
                    self._play_audio_bytes(ari_client, audio)

        # Max turns reached
        self._hangup(ari_client)

    def _transfer(self, ari_client, node: IVRNode):
        """Transfer call to agent or queue."""
        target = node.transfer_to or self.flow.transfer_extension or 'agents-queue'
        logger.info(f'IVR transfer: channel={self.channel_id} -> {target}')
        try:
            ari_client.channels.redirect(
                channelId=self.channel_id,
                endpoint=f'PJSIP/{target}',
            )
            self.transferred = True
        except Exception as exc:
            logger.error(f'IVR transfer failed: {exc}')
            self._hangup(ari_client)

    def _hangup(self, ari_client):
        """Hang up the channel."""
        self.completed = True
        try:
            ari_client.channels.hangup(channelId=self.channel_id)
        except Exception:
            pass

    def _play_audio_bytes(self, ari_client, audio_bytes: bytes):
        """Save audio to temp file and play via ARI."""
        import tempfile, os
        try:
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
                f.write(audio_bytes)
                path = f.name
            ari_client.channels.play(
                channelId=self.channel_id,
                media=f'sound:{path}',
            )
            time.sleep(0.5)  # Brief pause after playback starts
            os.unlink(path)
        except Exception as exc:
            logger.error(f'Play audio error: {exc}')

    def _play_audio_file(self, ari_client, file_path: str):
        """Play a pre-recorded audio file via ARI."""
        try:
            ari_client.channels.play(
                channelId=self.channel_id,
                media=f'sound:{file_path}',
            )
        except Exception as exc:
            logger.error(f'Play file error: {exc}')

    def _ari_say(self, ari_client, text: str):
        """Fallback: use Asterisk say to read text."""
        pass  # Would use AGI in production

    def _record_audio(self, ari_client, duration: int = 5) -> Optional[bytes]:
        """Record customer speech for STT. Returns audio bytes."""
        import tempfile, os
        try:
            path = tempfile.mktemp(suffix='.wav')
            ari_client.channels.record(
                channelId=self.channel_id,
                name=path, format='wav',
                maxDurationSeconds=duration,
                beep=False,
            )
            time.sleep(duration + 0.5)
            if os.path.exists(path):
                data = open(path, 'rb').read()
                os.unlink(path)
                return data
        except Exception as exc:
            logger.error(f'Record audio error: {exc}')
        return None


# ── Global Sarvam AI client instance ─────────────────────────────────────────
_sarvam_client = None


def get_sarvam_client() -> SarvamAIClient:
    global _sarvam_client
    if _sarvam_client is None:
        _sarvam_client = SarvamAIClient()
    return _sarvam_client


def start_ivr_session(channel_id: str, flow_id: int, lead_data: dict, ari_client) -> None:
    """Start an IVR session in a daemon thread."""
    try:
        flow    = IVRFlow.objects.get(id=flow_id, is_active=True)
        session = IVRSession(channel_id, flow, lead_data)
        t = threading.Thread(target=session.start, args=(ari_client,), daemon=True)
        t.start()
        logger.info(f'IVR session thread started: channel={channel_id} flow={flow.name}')
    except IVRFlow.DoesNotExist:
        logger.error(f'IVR flow not found: {flow_id}')
    except Exception as exc:
        logger.error(f'Failed to start IVR session: {exc}')
