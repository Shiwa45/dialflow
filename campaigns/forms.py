# campaigns/forms.py
from django import forms
from .models import Campaign, Disposition


class CampaignForm(forms.ModelForm):
    class Meta:
        model  = Campaign
        fields = [
            'name', 'description', 'asterisk_server', 'carrier',
            'caller_id', 'dial_prefix', 'dial_mode',
            'dial_ratio', 'min_dial_ratio', 'max_dial_ratio',
            'dial_timeout', 'abandon_rate',
            'hopper_level', 'hopper_size', 'lead_order',
            'amd_enabled', 'amd_action',
            'enable_recording', 'recording_mode',
            'call_hour_start', 'call_hour_end', 'respect_timezone',
            'wrapup_timeout', 'auto_wrapup_enabled',
            'auto_wrapup_timeout', 'auto_wrapup_disposition',
            'max_attempts', 'retry_delay_minutes',
            'dispositions',
            'use_system_dnc', 'use_campaign_dnc',
        ]
        widgets = {
            'description':       forms.Textarea(attrs={'rows': 3, 'class': 'form-input'}),
            'call_hour_start':   forms.TimeInput(attrs={'type': 'time', 'class': 'form-input'}),
            'call_hour_end':     forms.TimeInput(attrs={'type': 'time', 'class': 'form-input'}),
            'dispositions':      forms.CheckboxSelectMultiple(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, (forms.TextInput, forms.NumberInput,
                                          forms.Select, forms.Textarea)):
                field.widget.attrs.setdefault('class', 'form-input')
        # Only show active dispositions
        self.fields['dispositions'].queryset = Disposition.objects.filter(is_active=True)


class DispositionForm(forms.ModelForm):
    class Meta:
        model  = Disposition
        fields = ['name', 'category', 'outcome', 'color', 'hotkey', 'sort_order']
        widgets = {
            'color': forms.TextInput(attrs={'type': 'color', 'class': 'form-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-input')
