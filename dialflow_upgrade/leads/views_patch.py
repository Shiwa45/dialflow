# leads/views_patch.py
# ─────────────────────────────────────────────────────────────────────────────
# ADD this view function to leads/views.py
# ─────────────────────────────────────────────────────────────────────────────


@login_required
@require_POST
def toggle_dnc(request, pk):
    """Toggle DNC status for a lead. Available from lead list and detail."""
    lead = get_object_or_404(Lead, pk=pk)

    if lead.do_not_call:
        # Remove DNC
        lead.do_not_call = False
        lead.save(update_fields=['do_not_call', 'updated_at'])
        from campaigns.models import DNCEntry
        DNCEntry.objects.filter(phone_number=lead.primary_phone).delete()
        return JsonResponse({'success': True, 'dnc': False, 'message': 'Lead removed from DNC'})
    else:
        # Add DNC
        campaign_id = request.POST.get('campaign_id')
        reason = request.POST.get('reason', 'Manual DNC via UI')
        lead.mark_dnc(
            added_by=request.user,
            reason=reason,
            campaign_id=campaign_id,
        )
        return JsonResponse({'success': True, 'dnc': True, 'message': 'Lead added to DNC'})
