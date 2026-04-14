import re

with open('/home/shiwansh/dialer/templates/agents/dashboard.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix URL mappings to prevent NoReverseMatch in dialflow
replacements = {
    'agents:update_status': 'agents:set_status',
    'agents:set_disposition': 'agents:dispose',
    'agents:get_lead_info': 'agents:lead_info',
    'agents:call_status': 'agents:status',
    'agents:status_info': 'agents:status',
    # URLs that don't exist in dialflow's backend:
    "{% url 'agents:hangup' %}": "'#'",
    '{% url "agents:hangup" %}': "'#'",
    "{% url 'agents:wrapup_state' %}": "'#'",
    '{% url "agents:wrapup_state" %}': "'#'",
    "{% url 'agents:can_logout' %}": "'#'",
    '{% url "agents:can_logout" %}': "'#'",
    "{% url 'agents:select_campaign' %}": "'#'",
    '{% url "agents:select_campaign" %}': "'#'",
}

for old, new in replacements.items():
    content = content.replace(old, new)

with open('/home/shiwansh/dialflow/templates/agents/dashboard.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("Dashboard replaced successfully.")
