# DialFlow Pro — Upgrade Package

## Changes
1. **Login Time Current Date Only** — AgentLoginLog model, splits sessions at midnight
2. **Call Recording UI** — Inline audio player in call logs + agent history
3. **Telephony CRUD UI** — Full server/phone/carrier management (no Django admin needed)
4. **Role-Based Redirect** — Agent→dashboard, Admin/Supervisor→CRM
5. **Agent Call History** — Filters (date/status/search) + recording playback
6. **Advanced Reports** — 6 report types with CSV download (Disposition, Hourly, CDR, DNC, Agent, Campaign)
7. **CSV Import Field Mapping** — Already exists in codebase
8. **Lead DNC Toggle** — Toggle DNC per lead from list/detail
9. **Extras** — PauseCode model, sidebar sections, nightly Celery tasks

## Install
1. Copy files over existing project
2. Add toggle_dnc view from leads/views_patch.py to leads/views.py
3. Apply template patches from leads/list_dnc_patch.html
4. Run: python manage.py migrate agents && python manage.py migrate reports
5. Create pause codes: python manage.py shell (see agents/models_patch.py)
6. Add Celery beat tasks from reports/tasks.py
7. Restart services
