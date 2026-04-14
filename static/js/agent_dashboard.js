/**
 * Agent Dashboard JavaScript - IMPROVED with Phase 1 Fixes
 * 
 * Phase 1 Fixes Applied:
 * - 1.1: Handle call_incoming event for immediate UI update
 * - 1.2: Handle call_cleared event to wipe previous call data
 * 
 * This script handles:
 * - WebSocket connection for real-time events
 * - Call state management
 * - UI updates for incoming calls, connected calls, call end
 * - Disposition handling
 */

(function () {
    'use strict';

    // ========================================
    // State Management
    // ========================================

    const state = {
        agentId: null,
        currentCall: null,
        currentLead: null,
        callDurationTimer: null,
        status: 'offline',
        socket: null,
        reconnectAttempts: 0,
        maxReconnectAttempts: 10,
        // Phase 1.1: Auto-wrapup state
        wrapupTimer: null,
        wrapupTimeoutSeconds: null,
        wrapupStartTime: null,
        wrapupCountdown: null,
        urls: {
            status: '',
            callStatus: '',
            leadInfo: '',
            disposition: '',
            hangup: '',
            manualDial: '',
            transfer: ''
        }
    };

    // ========================================
    // Initialization
    // ========================================

    function init() {
        const dashboard = document.querySelector('[data-agent-dashboard]');
        if (!dashboard) {
            console.error('Agent dashboard element not found');
            return;
        }

        // Get configuration from data attributes
        state.agentId = dashboard.dataset.agentId;
        state.status = dashboard.dataset.status || 'offline';
        state.urls.status = dashboard.dataset.statusUrl;
        state.urls.callStatus = dashboard.dataset.callStatusUrl;
        state.urls.leadInfo = dashboard.dataset.leadInfoUrl;
        state.urls.disposition = dashboard.dataset.dispositionUrl;
        state.urls.hangup = dashboard.dataset.hangupUrl;
        state.urls.manualDial = dashboard.dataset.manualDialUrl;
        state.urls.transfer = dashboard.dataset.transferUrl;

        // Connect WebSocket
        connectWebSocket();

        // Setup event listeners
        setupEventListeners();

        // Initial UI state
        updateStatusUI(state.status);

        console.log('Agent dashboard initialized', { agentId: state.agentId });
    }

    // ========================================
    // WebSocket Connection
    // ========================================

    function connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/agent/${state.agentId}/`;

        console.log('Connecting to WebSocket:', wsUrl);

        try {
            state.socket = new WebSocket(wsUrl);

            state.socket.onopen = function (e) {
                console.log('WebSocket connected to agent channel');
                state.reconnectAttempts = 0;
                // Don't show toast on every reconnect, only on initial connection
                if (state.reconnectAttempts === 0) {
                    console.log('WebSocket connection established for real-time updates');
                }
            };

            state.socket.onmessage = function (e) {
                try {
                    const data = JSON.parse(e.data);
                    handleWebSocketMessage(data);
                } catch (error) {
                    console.error('Error parsing WebSocket message:', error);
                }
            };

            state.socket.onclose = function (e) {
                console.warn('WebSocket closed:', e.code, e.reason);
                scheduleReconnect();
            };

            state.socket.onerror = function (e) {
                console.error('WebSocket error:', e);
            };

        } catch (error) {
            console.error('Error creating WebSocket:', error);
            scheduleReconnect();
        }
    }

    function scheduleReconnect() {
        if (state.reconnectAttempts >= state.maxReconnectAttempts) {
            console.error('Max reconnection attempts reached');
            showToast('Connection lost. Please refresh the page.', 'error');
            return;
        }

        state.reconnectAttempts++;
        const delay = Math.min(1000 * Math.pow(2, state.reconnectAttempts), 30000);

        console.log(`Reconnecting in ${delay}ms (attempt ${state.reconnectAttempts})`);
        setTimeout(connectWebSocket, delay);
    }

    // ========================================
    // WebSocket Message Handling
    // ========================================

    function handleWebSocketMessage(data) {
        console.log('WebSocket message received on current page:', data);

        // Ensure we're handling this on the correct page
        const dashboard = document.querySelector('[data-agent-dashboard]');
        if (!dashboard) {
            console.warn('Dashboard element not found, ignoring message');
            return;
        }

        const eventType = data.type;

        switch (eventType) {
            // PHASE 1.1: NEW - Handle immediate call notification
            case 'call_incoming':
                handleCallIncoming(data);
                break;

            case 'call_connecting':
                handleCallConnecting(data);
                break;

            case 'call_connected':
                handleCallConnected(data);
                break;

            case 'call_ended':
                handleCallEnded(data);
                break;

            // PHASE 1.2: NEW - Handle call cleared after disposition
            case 'call_cleared':
                handleCallCleared(data);
                break;

            case 'status_update':
                handleStatusUpdate(data);
                break;

            case 'connection_established':
                console.log('Connection confirmed for agent:', data.username);
                break;

            case 'pong':
                // Heartbeat response
                break;

            // Phase 1.1: Auto-wrapup events
            case 'wrapup_started':
                handleWrapupStarted(data);
                break;

            case 'call_auto_disposed':
                handleCallAutoDisposed(data);
                break;

            default:
                console.log('Unknown event type:', eventType);
        }
    }

    // ========================================
    // PHASE 1.1: Immediate Call Display
    // ========================================

    /**
     * Handle call_incoming event - PHASE 1.1 FIX
     * This fires IMMEDIATELY when a call is placed, before it's answered
     * Allows UI to show "Incoming Call" with minimal delay
     */
    function handleCallIncoming(data) {
        console.log('Call incoming - updating UI immediately on current page:', data);

        const call = data.call || {};
        const lead = data.lead || {};

        // Store call data - handle both id (primary key) and call_id (UUID)
        state.currentCall = {
            id: call.id || call.call_id,  // Primary key ID
            call_id: call.call_id || call.id,  // UUID call_id field
            number: call.number,
            status: 'ringing',
            leadId: call.lead_id,
            startTime: new Date()
        };
        state.currentLead = lead;

        console.log('Stored call data:', state.currentCall);

        // IMMEDIATELY update UI on THIS page - don't wait for call_connected
        showCallPanel();
        updateCallDisplay({
            number: call.number,
            status: 'Incoming Call...',
            statusClass: 'ringing'
        });

        // Update phone display if available
        const phoneDisplay = document.getElementById('phone-status-display');
        const phoneNumberDisplay = document.getElementById('phone-number-display');
        if (phoneDisplay) phoneDisplay.textContent = 'RINGING';
        if (phoneNumberDisplay && call.number) phoneNumberDisplay.textContent = call.number;

        // Show lead info if available
        if (lead && lead.id) {
            updateLeadDisplay(lead);
        } else if (call.lead_id) {
            // Fetch lead info asynchronously
            fetchLeadInfo(call.lead_id);
        }

        // Play ring sound/notification
        playNotificationSound();
        showToast('Incoming call: ' + call.number, 'info');

        // Update status to show we're receiving a call
        updateStatusUI('ringing');
    }

    /**
     * Handle call_connecting event
     * Customer leg is being connected to agent
     */
    function handleCallConnecting(data) {
        console.log('Call connecting:', data);

        const call = data.call || {};

        if (state.currentCall) {
            state.currentCall.status = 'connecting';
        }

        updateCallDisplay({
            number: call.number,
            status: 'Connecting...',
            statusClass: 'connecting'
        });
    }

    /**
     * Handle call_connected event
     * Call is now fully connected (answered)
     */
    function handleCallConnected(data) {
        console.log('Call connected:', data);

        const call = data.call || {};
        const lead = data.lead || {};

        // Update call state - preserve existing call data and update with new info
        state.currentCall = {
            id: call.id || state.currentCall?.id || call.call_id,
            call_id: call.call_id || state.currentCall?.call_id || call.id,
            number: call.number || state.currentCall?.number,
            status: 'connected',
            leadId: call.lead_id || state.currentCall?.leadId,
            startTime: state.currentCall?.startTime || new Date(),
            answerTime: new Date()
        };

        console.log('Updated call data on connect:', state.currentCall);

        // Update lead if provided
        if (lead && lead.id) {
            state.currentLead = lead;
            updateLeadDisplay(lead);
        }

        // Update UI
        showCallPanel();
        updateCallDisplay({
            number: call.number,
            status: 'Connected',
            statusClass: 'connected'
        });

        // Start duration timer
        startDurationTimer();

        // Enable disposition button
        enableDispositionButton(true);

        // Update agent status
        updateStatusUI('busy');

        showToast('Call connected', 'success');
    }

    // ========================================
    // PHASE 1.2: Call Cleared After Disposition
    // ========================================

    /**
     * Handle call_cleared event - PHASE 1.2 FIX
     * This fires AFTER disposition is saved
     * Ensures UI is completely wiped of previous call data
     */
    function handleCallCleared(data) {
        console.log('Call cleared:', data);

        // Stop duration timer
        stopDurationTimer();

        // Clear ALL call state
        state.currentCall = null;
        state.currentLead = null;

        // COMPLETELY clear the UI
        clearCallUI();

        // Close disposition modal if open
        closeDispositionModal();

        // Show success message
        if (data.disposition) {
            showToast(`Call dispositioned: ${data.disposition}`, 'success');
        }

        // Update status to available
        updateStatusUI('available');
    }

    /**
     * Clear all call-related UI elements - PHASE 1.2 FIX
     * This ensures no stale data remains on screen
     */
    function clearCallUI() {
        // Hide call details panel
        const callDetails = document.querySelector('[data-call-details]');
        const callPlaceholder = document.querySelector('[data-call-placeholder]');

        if (callDetails) callDetails.hidden = true;
        if (callPlaceholder) callPlaceholder.hidden = false;

        // Reset call info fields
        const callNumber = document.querySelector('[data-call-number]');
        const callState = document.querySelector('[data-call-state]');
        const callDuration = document.querySelector('[data-call-duration]');

        if (callNumber) callNumber.textContent = '—';
        if (callState) {
            callState.textContent = 'Idle';
            callState.className = 'call-status-badge';
        }
        if (callDuration) callDuration.textContent = '00:00';

        // Reset lead card
        const leadCard = document.querySelector('[data-lead-card]');
        if (leadCard) leadCard.hidden = true;

        const leadName = document.querySelector('[data-lead-name]');
        const leadPhone = document.querySelector('[data-lead-phone]');
        const leadEmail = document.querySelector('[data-lead-email]');
        const leadCompany = document.querySelector('[data-lead-company]');
        const leadLocation = document.querySelector('[data-lead-location]');
        const leadStatus = document.querySelector('[data-lead-status]');

        if (leadName) leadName.textContent = 'Lead name';
        if (leadPhone) leadPhone.textContent = '—';
        if (leadEmail) leadEmail.textContent = '—';
        if (leadCompany) leadCompany.textContent = '—';
        if (leadLocation) leadLocation.textContent = '—';
        if (leadStatus) leadStatus.textContent = 'Unknown';

        // Disable disposition button
        enableDispositionButton(false);

        console.log('Call UI cleared');
    }

    // ========================================
    // Call End Handling
    // ========================================

    function handleCallEnded(data) {
        console.log('Call ended:', data);

        // CRITICAL FIX: Handle force_disconnect flag for immediate UI update
        if (data.force_disconnect) {
            console.log('[FORCE DISCONNECT] Client hung up - updating UI immediately');
            updateCallDisplay({
                status: 'Disconnected',
                statusClass: 'ended'
            });
            updateStatusUI('wrapup');
        }

        // Stop duration timer
        stopDurationTimer();

        // Update call state - preserve call ID from data if provided
        if (state.currentCall) {
            state.currentCall.status = 'ended';
            state.currentCall.endTime = new Date();
            // Update call ID if provided in the event
            if (data.call && data.call.id) {
                state.currentCall.id = data.call.id;
            }
            if (data.call && data.call.call_id) {
                state.currentCall.call_id = data.call.call_id;
            }
        } else if (data.call) {
            // Create call state if it doesn't exist
            state.currentCall = {
                id: data.call.id || data.call.call_id,
                call_id: data.call.call_id || data.call.id,
                number: data.call.number,
                status: 'ended',
                endTime: new Date()
            };
        }

        console.log('Call state after ended:', state.currentCall);

        // Update UI to show call ended
        updateCallDisplay({
            status: 'Call Ended',
            statusClass: 'ended'
        });

        // If disposition is needed, show modal
        if (data.disposition_needed) {
            showDispositionModal();
            updateStatusUI('wrapup');
        }

        showToast(data.message || 'Call ended', 'info');
    }

    // ========================================
    // Phase 1.1: Auto-Wrapup Handlers
    // ========================================

    /**
     * Handle wrapup started event
     */
    function handleWrapupStarted(data) {
        console.log('Wrapup started:', data);

        state.wrapupTimeoutSeconds = data.timeout_seconds;
        state.wrapupStartTime = Date.now();

        // Show wrapup countdown
        showWrapupCountdown(data.timeout_seconds);

        // Start countdown timer
        startWrapupCountdown();
    }

    /**
     * Show wrapup countdown UI
     */
    function showWrapupCountdown(timeoutSeconds) {
        const dispositionPanel = document.getElementById('disposition-modal');
        if (!dispositionPanel) return;

        // Create countdown display if it doesn't exist
        let countdownDiv = document.getElementById('wrapup-countdown');
        if (!countdownDiv) {
            countdownDiv = document.createElement('div');
            countdownDiv.id = 'wrapup-countdown';
            countdownDiv.className = 'alert alert-warning mt-3';
            dispositionPanel.insertBefore(countdownDiv, dispositionPanel.firstChild);
        }

        countdownDiv.innerHTML = `
            <div class="d-flex align-items-center">
                <i class="fas fa-clock me-2"></i>
                <div class="flex-grow-1">
                    <strong>Auto-completing in:</strong>
                    <span id="countdown-timer" class="ms-2 fs-5 fw-bold"></span>
                </div>
            </div>
            <div class="progress mt-2" style="height: 8px;">
                <div id="countdown-progress" class="progress-bar progress-bar-striped progress-bar-animated bg-warning" 
                     role="progressbar" style="width: 100%"></div>
            </div>
            <small class="text-muted mt-2 d-block">
                Dispose the call manually to cancel auto-completion
            </small>
        `;

        // Update initial countdown display
        updateCountdownDisplay(timeoutSeconds);
    }

    /**
     * Start countdown timer
     */
    function startWrapupCountdown() {
        // Clear any existing timer
        if (state.wrapupCountdown) {
            clearInterval(state.wrapupCountdown);
        }

        // Update every second
        state.wrapupCountdown = setInterval(() => {
            const elapsed = Math.floor((Date.now() - state.wrapupStartTime) / 1000);
            const remaining = Math.max(0, state.wrapupTimeoutSeconds - elapsed);

            updateCountdownDisplay(remaining);

            // Stop when reaches zero
            if (remaining <= 0) {
                clearInterval(state.wrapupCountdown);
                state.wrapupCountdown = null;
            }
        }, 1000);
    }

    /**
     * Update countdown display
     */
    function updateCountdownDisplay(remainingSeconds) {
        const timerElement = document.getElementById('countdown-timer');
        const progressElement = document.getElementById('countdown-progress');

        if (!timerElement || !progressElement) return;

        // Format time as MM:SS
        const minutes = Math.floor(remainingSeconds / 60);
        const seconds = remainingSeconds % 60;
        const timeString = `${minutes}:${seconds.toString().padStart(2, '0')}`;

        timerElement.textContent = timeString;

        // Update progress bar
        const percentage = (remainingSeconds / state.wrapupTimeoutSeconds) * 100;
        progressElement.style.width = `${percentage}%`;

        // Change color based on remaining time
        if (remainingSeconds <= 10) {
            progressElement.classList.remove('bg-warning');
            progressElement.classList.add('bg-danger');
        } else if (remainingSeconds <= 30) {
            progressElement.classList.remove('bg-warning', 'bg-danger');
            progressElement.classList.add('bg-warning');
        }
    }

    /**
     * Hide wrapup countdown
     */
    function hideWrapupCountdown() {
        const countdownDiv = document.getElementById('wrapup-countdown');
        if (countdownDiv) {
            countdownDiv.remove();
        }

        // Clear timer
        if (state.wrapupCountdown) {
            clearInterval(state.wrapupCountdown);
            state.wrapupCountdown = null;
        }

        state.wrapupTimeoutSeconds = null;
        state.wrapupStartTime = null;
    }

    /**
     * Handle call auto-disposed event
     */
    function handleCallAutoDisposed(data) {
        console.log('Call auto-disposed:', data);

        // Hide countdown
        hideWrapupCountdown();

        // Show notification
        showToast(
            `Call Auto-Dispositioned as: ${data.disposition}`,
            'info'
        );

        // Clear call UI
        clearCallUI();

        // Update status to available
        updateStatusUI('available');
    }


    // ========================================
    // Status Updates
    // ========================================

    function handleStatusUpdate(data) {
        console.log('Status update received via WebSocket:', data);

        // Only update if status actually changed
        if (state.status !== data.status) {
            state.status = data.status;
            updateStatusUI(data.status);

            if (data.message) {
                showToast(data.message, 'info');
            } else {
                showToast(`Status updated to ${getStatusDisplayText(data.status)}`, 'info');
            }
        }
    }

    function updateStatusUI(status) {
        // Update status badge/pill - try both selectors for compatibility
        const statusBadge = document.querySelector('[data-current-status]') || document.querySelector('[data-agent-status]');
        if (statusBadge) {
            const statusDisplay = getStatusDisplayText(status);
            statusBadge.textContent = statusDisplay;
            // Keep existing classes but update status-related ones
            statusBadge.className = statusBadge.className.replace(/status-\w+/g, '');
            statusBadge.classList.add(`status-${status}`);
        }

        // Update status buttons
        const statusButtons = document.querySelectorAll('[data-status-trigger]');
        statusButtons.forEach(btn => {
            const btnStatus = btn.dataset.statusTrigger;
            btn.classList.toggle('is-active', btnStatus === status);
        });

        // Update body data attribute for CSS
        document.body.dataset.agentStatus = status;

        // Also update the dashboard data attribute
        const dashboard = document.querySelector('[data-agent-dashboard]');
        if (dashboard) {
            dashboard.dataset.status = status;
        }
    }

    function getStatusDisplayText(status) {
        const statusMap = {
            'available': 'Available',
            'break': 'Break',
            'lunch': 'Lunch',
            'training': 'Training',
            'meeting': 'Meeting',
            'offline': 'Offline',
            'busy': 'Busy',
            'wrapup': 'Wrap Up',
            'ringing': 'Ringing',
            'paused': 'Paused'
        };
        return statusMap[status] || capitalizeFirst(status);
    }

    // ========================================
    // UI Updates
    // ========================================

    function showCallPanel() {
        const callDetails = document.querySelector('[data-call-details]');
        const callPlaceholder = document.querySelector('[data-call-placeholder]');

        if (callDetails) callDetails.hidden = false;
        if (callPlaceholder) callPlaceholder.hidden = true;
    }

    function updateCallDisplay(options) {
        const { number, status, statusClass } = options;

        if (number) {
            const callNumber = document.querySelector('[data-call-number]');
            if (callNumber) callNumber.textContent = number;
        }

        if (status) {
            const callState = document.querySelector('[data-call-state]');
            if (callState) {
                callState.textContent = status;
                if (statusClass) {
                    callState.className = `call-status-badge status-${statusClass}`;
                }
            }
        }
    }

    function updateLeadDisplay(lead) {
        const leadCard = document.querySelector('[data-lead-card]');
        if (leadCard) leadCard.hidden = false;

        const fields = {
            '[data-lead-name]': `${lead.first_name || ''} ${lead.last_name || ''}`.trim() || 'Unknown',
            '[data-lead-phone]': lead.phone || lead.phone_number || '—',
            '[data-lead-email]': lead.email || '—',
            '[data-lead-company]': lead.company || '—',
            '[data-lead-location]': [lead.city, lead.state].filter(Boolean).join(', ') || '—',
            '[data-lead-status]': lead.status || 'Unknown'
        };

        Object.entries(fields).forEach(([selector, value]) => {
            const el = document.querySelector(selector);
            if (el) el.textContent = value;
        });
    }

    // ========================================
    // Duration Timer
    // ========================================

    function startDurationTimer() {
        stopDurationTimer(); // Clear any existing timer

        const durationEl = document.querySelector('[data-call-duration]');
        if (!durationEl) return;

        const startTime = state.currentCall?.answerTime || new Date();

        state.callDurationTimer = setInterval(() => {
            const elapsed = Math.floor((new Date() - startTime) / 1000);
            const minutes = Math.floor(elapsed / 60).toString().padStart(2, '0');
            const seconds = (elapsed % 60).toString().padStart(2, '0');
            durationEl.textContent = `${minutes}:${seconds}`;
        }, 1000);
    }

    function stopDurationTimer() {
        if (state.callDurationTimer) {
            clearInterval(state.callDurationTimer);
            state.callDurationTimer = null;
        }
    }

    // ========================================
    // Disposition Handling
    // ========================================

    function showDispositionModal() {
        const modal = document.getElementById('disposition-modal');
        if (modal) {
            modal.hidden = false;

            // Set call ID in form - prefer primary key id, fallback to call_id UUID
            const callIdInput = document.getElementById('disposition-call-id');
            if (callIdInput && state.currentCall) {
                // Use primary key id if available, otherwise use call_id UUID
                const callIdToUse = state.currentCall.id || state.currentCall.call_id;
                callIdInput.value = callIdToUse;
                console.log('Disposition modal opened with call ID:', callIdToUse, 'Full call data:', state.currentCall);
            } else {
                console.error('Cannot show disposition modal: call ID missing', {
                    hasInput: !!callIdInput,
                    hasCall: !!state.currentCall,
                    callId: state.currentCall?.id,
                    call_id: state.currentCall?.call_id
                });
                showToast('Error: Call information is missing. Please refresh the page.', 'error');
                return;
            }

            // Reset form state
            const submitBtn = modal.querySelector('button[type="submit"]');
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.textContent = 'Save';
            }

            // Focus on disposition select
            const dispositionSelect = document.getElementById('disposition-select');
            if (dispositionSelect) {
                setTimeout(() => dispositionSelect.focus(), 100);
            }
        } else {
            console.error('Disposition modal not found');
        }
    }

    function closeDispositionModal() {
        const modal = document.getElementById('disposition-modal');
        if (modal) {
            modal.hidden = true;

            // Reset form
            const form = document.getElementById('disposition-form');
            if (form) form.reset();
        }
    }

    function enableDispositionButton(enabled) {
        const btn = document.querySelector('[data-open-disposition]');
        if (btn) {
            btn.disabled = !enabled;
        }
    }

    function submitDisposition(callId, dispositionId, notes) {
        console.log('submitDisposition called with:', { callId, dispositionId, notes, url: state.urls.disposition });

        if (!state.urls.disposition) {
            console.error('Disposition URL is not configured');
            showToast('Error: Disposition URL not configured', 'error');
            return;
        }

        // Phase 1.1: Cancel wrapup timer on manual disposition
        hideWrapupCountdown();

        // Disable submit button to prevent double submission
        const submitBtn = document.querySelector('#disposition-form button[type="submit"]');
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = 'Saving...';
        }

        const formData = new FormData();
        formData.append('call_id', callId);
        formData.append('disposition_id', dispositionId);
        formData.append('notes', notes || '');

        console.log('Sending disposition request to:', state.urls.disposition);

        fetch(state.urls.disposition, {
            method: 'POST',
            body: formData,
            headers: {
                'X-CSRFToken': getCSRFToken()
            }
        })
            .then(response => {
                console.log('Response status:', response.status);
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                return response.json();
            })
            .then(data => {
                console.log('Disposition response:', data);
                if (data.success) {
                    // The call_cleared event will handle UI cleanup
                    console.log('Disposition submitted successfully');
                    showToast('Disposition saved successfully', 'success');
                    closeDispositionModal();
                } else {
                    console.error('Disposition submission failed:', data.error);
                    showToast(data.error || 'Failed to save disposition', 'error');
                    // Re-enable submit button
                    if (submitBtn) {
                        submitBtn.disabled = false;
                        submitBtn.textContent = 'Save';
                    }
                }
            })
            .catch(error => {
                console.error('Disposition error:', error);
                showToast('Failed to save disposition: ' + error.message, 'error');
                // Re-enable submit button
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.textContent = 'Save';
                }
            });
    }

    // ========================================
    // API Calls
    // ========================================

    function fetchLeadInfo(leadId) {
        if (!leadId || !state.urls.leadInfo) return;

        fetch(`${state.urls.leadInfo}?lead_id=${leadId}`)
            .then(response => response.json())
            .then(data => {
                if (data.success && data.lead) {
                    state.currentLead = data.lead;
                    updateLeadDisplay(data.lead);
                }
            })
            .catch(error => {
                console.error('Error fetching lead info:', error);
            });
    }

    function updateAgentStatus(newStatus) {
        // Immediately update UI on current page (optimistic update)
        const previousStatus = state.status;
        state.status = newStatus;
        updateStatusUI(newStatus);

        const formData = new FormData();
        formData.append('status', newStatus);

        fetch(state.urls.status, {
            method: 'POST',
            body: formData,
            headers: {
                'X-CSRFToken': getCSRFToken()
            }
        })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    // Status already updated optimistically, just confirm
                    state.status = data.status;
                    updateStatusUI(data.status);
                    showToast(`Status updated to ${getStatusDisplayText(data.status)}`, 'success');
                } else {
                    // Revert on error
                    state.status = previousStatus;
                    updateStatusUI(previousStatus);
                    showToast(data.error || 'Failed to update status', 'error');
                }
            })
            .catch(error => {
                console.error('Status update error:', error);
                // Revert on error
                state.status = previousStatus;
                updateStatusUI(previousStatus);
                showToast('Failed to update status', 'error');
            });
    }

    function hangupCall() {
        if (!state.currentCall) return;

        const formData = new FormData();
        formData.append('call_id', state.currentCall.id);

        fetch(state.urls.hangup, {
            method: 'POST',
            body: formData,
            headers: {
                'X-CSRFToken': getCSRFToken()
            }
        })
            .then(response => response.json())
            .then(data => {
                if (!data.success) {
                    showToast(data.error || 'Failed to hangup call', 'error');
                }
            })
            .catch(error => {
                console.error('Hangup error:', error);
                showToast('Failed to hangup call', 'error');
            });
    }

    function performManualDial(number) {
        if (!state.urls.manualDial) {
            console.error('Manual dial URL not configured');
            return;
        }

        showToast(`Dialing ${number}...`, 'info');

        const formData = new FormData();
        formData.append('phone_number', number);

        fetch(state.urls.manualDial, {
            method: 'POST',
            body: formData,
            headers: {
                'X-CSRFToken': getCSRFToken()
            }
        })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    console.log('Manual dial initiated');
                } else {
                    showToast(data.error || 'Failed to dial', 'error');
                }
            })
            .catch(error => {
                console.error('Manual dial error:', error);
                showToast('Error initiating call', 'error');
            });
    }

    // ========================================
    // Event Listeners
    // ========================================

    function setupEventListeners() {
        // Status buttons
        document.querySelectorAll('[data-status-trigger]').forEach(btn => {
            btn.addEventListener('click', (event) => {
                event.preventDefault();
                const status = btn.dataset.statusTrigger;
                updateAgentStatus(status);
            });
        });

        // Disposition button
        const dispositionBtn = document.querySelector('[data-open-disposition]');
        if (dispositionBtn) {
            dispositionBtn.addEventListener('click', showDispositionModal);
        }

        // Disposition form - use event delegation to ensure it works even if modal is hidden
        document.addEventListener('submit', (e) => {
            if (e.target && e.target.id === 'disposition-form') {
                e.preventDefault();
                e.stopPropagation();

                console.log('Disposition form submitted');

                const callId = document.getElementById('disposition-call-id')?.value;
                const dispositionId = document.getElementById('disposition-select')?.value;
                const notes = document.getElementById('disposition-notes')?.value;

                console.log('Form values:', { callId, dispositionId, notes });

                if (!callId) {
                    console.error('Call ID is missing');
                    showToast('Error: Call ID is missing. Please refresh the page.', 'error');
                    return;
                }

                if (!dispositionId) {
                    console.error('Disposition is not selected');
                    showToast('Please select a disposition', 'error');
                    return;
                }

                console.log('Submitting disposition...');
                submitDisposition(callId, dispositionId, notes);
            }
        });

        // Cancel disposition
        const cancelDisposition = document.querySelector('[data-cancel-disposition]');
        if (cancelDisposition) {
            cancelDisposition.addEventListener('click', closeDispositionModal);
        }

        // Hangup button
        const hangupBtn = document.querySelector('[data-hangup-btn]');
        if (hangupBtn) {
            hangupBtn.addEventListener('click', hangupCall);
        }

        // Manual Dial Handling
        const manualDialBtn = document.getElementById('manual-dial-btn');
        const manualDialInput = document.getElementById('manual-dial-input');
        if (manualDialBtn && manualDialInput) {
            manualDialBtn.addEventListener('click', () => {
                const number = manualDialInput.value.trim();
                if (number) {
                    performManualDial(number);
                }
            });

            // Allow Enter key to dial
            manualDialInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    const number = manualDialInput.value.trim();
                    if (number) {
                        performManualDial(number);
                    }
                }
            });
        }

        // Tab switching
        document.querySelectorAll('.tab-button').forEach(btn => {
            btn.addEventListener('click', () => {
                const tabId = btn.dataset.tab;
                switchTab(tabId);
            });
        });
    }

    function switchTab(tabId) {
        // Update buttons
        document.querySelectorAll('.tab-button').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tab === tabId);
        });

        // Update content
        document.querySelectorAll('.tab-content').forEach(content => {
            content.classList.toggle('active', content.id === `tab-${tabId}`);
        });
    }

    // ========================================
    // Utilities
    // ========================================

    function getCSRFToken() {
        // Try to get from cookie first
        const cookie = document.cookie
            .split('; ')
            .find(row => row.startsWith('csrftoken='));
        if (cookie) {
            return cookie.split('=')[1];
        }

        // Fallback to meta tag
        const metaTag = document.querySelector('meta[name="csrf-token"]');
        if (metaTag) {
            return metaTag.getAttribute('content');
        }

        console.error('CSRF token not found in cookie or meta tag');
        return '';
    }

    function capitalizeFirst(str) {
        return str ? str.charAt(0).toUpperCase() + str.slice(1) : '';
    }

    function showToast(message, type = 'info') {
        const toast = document.querySelector('[data-status-toast]');
        if (toast) {
            toast.textContent = message;
            toast.className = `status-toast show toast-${type}`;

            setTimeout(() => {
                toast.classList.remove('show');
            }, 3000);
        }
    }

    function playNotificationSound() {
        // Create audio element for notification
        try {
            const audio = new Audio('/static/sounds/notification.mp3');
            audio.volume = 0.5;
            audio.play().catch(() => {
                // Autoplay might be blocked
                console.log('Audio autoplay blocked');
            });
        } catch (e) {
            console.log('Could not play notification sound');
        }
    }

    // ========================================
    // Initialize on DOM Ready
    // ========================================

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Expose for debugging
    window.AgentDashboard = {
        state,
        clearCallUI,
        updateStatusUI,
        showDispositionModal
    };

})();
