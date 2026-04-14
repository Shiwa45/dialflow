/**
 * WebRTC Phone Integration for Agent Dashboard
 * Integrates JsSIP WebRTC functionality with retro phone UI
 */

// WebRTC Phone Instance
let webrtcPhone = null;
let dialedNumber = '';
let callTimer = null;
let callStartTime = null;

// Initialize WebRTC Phone on page load
document.addEventListener('DOMContentLoaded', function () {
    initializeWebRTCPhone();
});

/**
 * Initialize WebRTC Phone with SIP credentials
 */
function initializeWebRTCPhone() {
    // WebRTC Configuration - will be provided by Django template
    const config = {
        wsServer: window.WEBRTC_CONFIG?.wsServer || 'wss://localhost:8089/ws',
        sipUri: window.WEBRTC_CONFIG?.sipUri || 'sip:1001@localhost',
        password: window.WEBRTC_CONFIG?.password || '',
        displayName: window.WEBRTC_CONFIG?.displayName || 'Agent',
        stunServers: ['stun:stun.l.google.com:19302'],
        debug: true
    };

    console.log('Initializing WebRTC Phone...', config);

    try {
        // Check if JsSIP is loaded
        if (typeof JsSIP === 'undefined') {
            console.error('JsSIP library not loaded!');
            updateRetroStatus('ERROR: JsSIP not loaded');

            // Show user-friendly error
            setTimeout(() => {
                alert('WebRTC library failed to load. Please check your internet connection and refresh the page.');
            }, 1000);
            return;
        }

        console.log('JsSIP version:', JsSIP.version || 'unknown');

        // Configure JsSIP
        const socket = new JsSIP.WebSocketInterface(config.wsServer);

        const configuration = {
            sockets: [socket],
            uri: config.sipUri,
            password: config.password,
            display_name: config.displayName,
            register: true,
            session_timers: false
        };

        // Create User Agent
        webrtcPhone = new JsSIP.UA(configuration);

        // Set up event handlers
        setupWebRTCEventHandlers();

        // Start the UA
        webrtcPhone.start();

        updateRetroStatus('CONNECTING...');
        console.log('WebRTC Phone started');

    } catch (error) {
        console.error('Error initializing WebRTC:', error);
        updateRetroStatus('ERROR: ' + error.message);
    }
}

/**
 * Set up WebRTC event handlers
 */
function setupWebRTCEventHandlers() {
    // Registration events
    webrtcPhone.on('registered', function (e) {
        console.log('WebRTC: Registered');
        updateRetroStatus('READY');
        document.getElementById('ledRegistered').classList.add('active');
    });

    webrtcPhone.on('unregistered', function (e) {
        console.log('WebRTC: Unregistered');
        updateRetroStatus('OFFLINE');
        document.getElementById('ledRegistered').classList.remove('active');
    });

    webrtcPhone.on('registrationFailed', function (e) {
        console.error('WebRTC: Registration failed', e.cause);
        updateRetroStatus('REG FAILED');
        document.getElementById('ledRegistered').classList.remove('active');
    });

    // Incoming call
    webrtcPhone.on('newRTCSession', function (e) {
        const session = e.session;

        if (session.direction === 'incoming') {
            handleIncomingCall(session);
        }
    });

    // Connection events
    webrtcPhone.on('connected', function () {
        console.log('WebRTC: WebSocket connected');
    });

    webrtcPhone.on('disconnected', function () {
        console.log('WebRTC: WebSocket disconnected');
        updateRetroStatus('DISCONNECTED');
    });
}

/**
 * Handle incoming call
 */
let currentSession = null;

function handleIncomingCall(session) {
    console.log('Incoming call from:', session.remote_identity.uri.user);

    currentSession = session;
    const callerNumber = session.remote_identity.uri.user;

    // Update UI
    updateRetroNumber(callerNumber);
    updateRetroStatus('INCOMING CALL');
    document.getElementById('ledRinging').classList.add('active');
    document.getElementById('retroHandset').classList.add('ringing');

    // Show answer button, hide hangup
    document.getElementById('btnAnswer').style.display = 'block';
    document.getElementById('btnHangup').style.display = 'none';

    // Set up session handlers
    setupSessionHandlers(session);

    // Play ringtone (if available)
    playSound('ringtone');
}

/**
 * Answer incoming call
 */
function answerCall() {
    if (!currentSession) {
        console.error('No incoming call to answer');
        return;
    }

    try {
        const answerOptions = {
            mediaConstraints: {
                audio: true,
                video: false
            },
            pcConfig: {
                iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
            }
        };

        currentSession.answer(answerOptions);
        console.log('Call answered');

        // Update UI
        document.getElementById('ledRinging').classList.remove('active');
        document.getElementById('retroHandset').classList.remove('ringing');
        stopSound('ringtone');

    } catch (error) {
        console.error('Error answering call:', error);
    }
}

/**
 * Make outgoing call
 */
function dialpadPress(digit) {
    // If in call, send DTMF
    if (currentSession && currentSession.isEstablished()) {
        sendDTMF(digit);
        return;
    }

    // Otherwise add to dialed number
    dialedNumber += digit;
    updateRetroNumber(dialedNumber);

    // Play dial tone
    playSound('dtmf');
}

function backspaceNumber() {
    dialedNumber = dialedNumber.slice(0, -1);
    updateRetroNumber(dialedNumber || '—');
}

function makeCall() {
    if (!dialedNumber) {
        console.log('No number to dial');
        return;
    }

    if (!webrtcPhone || !webrtcPhone.isRegistered()) {
        alert('Phone not registered. Please wait...');
        return;
    }

    try {
        const callOptions = {
            mediaConstraints: {
                audio: true,
                video: false
            },
            pcConfig: {
                iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
            }
        };

        // Extract domain from SIP URI
        const sipUri = webrtcPhone._configuration.uri;
        const domain = sipUri.match(/@(.+)$/)[1];

        currentSession = webrtcPhone.call(`sip:${dialedNumber}@${domain}`, callOptions);

        setupSessionHandlers(currentSession);

        console.log('Calling:', dialedNumber);
        updateRetroStatus('CALLING...');
        document.getElementById('ledRinging').classList.add('active');

    } catch (error) {
        console.error('Error making call:', error);
        alert('Error making call: ' + error.message);
    }
}

/**
 * Hangup call
 */
function hangupCall() {
    if (!currentSession) {
        return;
    }

    try {
        currentSession.terminate();
        console.log('Call hung up');
    } catch (error) {
        console.error('Error hanging up:', error);
    }
}

/**
 * Toggle mute
 */
let isMuted = false;

function toggleMute() {
    if (!currentSession) return;

    try {
        if (isMuted) {
            currentSession.unmute({ audio: true });
            isMuted = false;
            document.getElementById('btnMute').classList.remove('active');
            document.getElementById('btnMute').querySelector('.material-icons').textContent = 'mic';
        } else {
            currentSession.mute({ audio: true });
            isMuted = true;
            document.getElementById('btnMute').classList.add('active');
            document.getElementById('btnMute').querySelector('.material-icons').textContent = 'mic_off';
        }
        console.log('Mute:', isMuted);
    } catch (error) {
        console.error('Error toggling mute:', error);
    }
}

/**
 * Toggle hold
 */
let isHeld = false;

function toggleHold() {
    if (!currentSession) return;

    try {
        if (isHeld) {
            currentSession.unhold();
            isHeld = false;
            document.getElementById('btnHold').classList.remove('active');
            document.getElementById('btnHold').querySelector('.material-icons').textContent = 'pause';
        } else {
            currentSession.hold();
            isHeld = true;
            document.getElementById('btnHold').classList.add('active');
            document.getElementById('btnHold').querySelector('.material-icons').textContent = 'play_arrow';
        }
        console.log('Hold:', isHeld);
    } catch (error) {
        console.error('Error toggling hold:', error);
    }
}

/**
 * Send DTMF tone
 */
function sendDTMF(tone) {
    if (!currentSession) return;

    try {
        currentSession.sendDTMF(tone);
        console.log('Sent DTMF:', tone);
    } catch (error) {
        console.error('Error sending DTMF:', error);
    }
}

/**
 * Set up session event handlers
 */
function setupSessionHandlers(session) {
    // Call progress
    session.on('progress', function (e) {
        console.log('Call progress');
        updateRetroStatus('RINGING...');
    });

    // Call confirmed (connected)
    session.on('confirmed', function (e) {
        console.log('Call confirmed');
        updateRetroStatus('CONNECTED');

        // Update UI
        document.getElementById('ledRinging').classList.remove('active');
        document.getElementById('ledInCall').classList.add('active');
        document.getElementById('btnAnswer').style.display = 'none';
        document.getElementById('btnHangup').style.display = 'block';
        document.getElementById('btnMute').disabled = false;
        document.getElementById('btnHold').disabled = false;

        // Start call timer
        startCallTimer();

        // Clear dialed number
        dialedNumber = '';
    });

    // Call ended
    session.on('ended', function (e) {
        console.log('Call ended:', e.cause);
        handleCallEnded();
    });

    // Call failed
    session.on('failed', function (e) {
        console.log('Call failed:', e.cause);
        handleCallEnded();
    });

    // Handle media (audio stream)
    session.on('peerconnection', function (e) {
        const peerconnection = e.peerconnection;

        peerconnection.ontrack = function (event) {
            console.log('Received remote track');
            // Attach remote audio
            const remoteAudio = document.getElementById('remoteAudio') || createAudioElement('remoteAudio');
            remoteAudio.srcObject = event.streams[0];
        };
    });
}

/**
 * Handle call ended
 */
function handleCallEnded() {
    // Stop call timer
    stopCallTimer();

    // Reset UI
    updateRetroStatus('READY');
    updateRetroNumber('—');
    document.getElementById('ledRinging').classList.remove('active');
    document.getElementById('ledInCall').classList.remove('active');
    document.getElementById('retroHandset').classList.remove('ringing');
    document.getElementById('btnAnswer').style.display = 'none';
    document.getElementById('btnHangup').style.display = 'none';
    document.getElementById('btnMute').disabled = true;
    document.getElementById('btnHold').disabled = true;
    document.getElementById('btnMute').classList.remove('active');
    document.getElementById('btnHold').classList.remove('active');

    // Reset state
    currentSession = null;
    isMuted = false;
    isHeld = false;
    dialedNumber = '';

    // Hide timer
    document.getElementById('retroTimer').style.display = 'none';
}

/**
 * Start call timer
 */
function startCallTimer() {
    callStartTime = new Date();
    document.getElementById('retroTimer').style.display = 'block';

    callTimer = setInterval(function () {
        const duration = Math.floor((new Date() - callStartTime) / 1000);
        const minutes = Math.floor(duration / 60);
        const seconds = duration % 60;
        document.getElementById('retroTimer').textContent =
            `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
    }, 1000);
}

/**
 * Stop call timer
 */
function stopCallTimer() {
    if (callTimer) {
        clearInterval(callTimer);
        callTimer = null;
    }
    callStartTime = null;
}

/**
 * Update retro phone display
 */
function updateRetroNumber(number) {
    document.getElementById('retroNumber').textContent = number;
}

function updateRetroStatus(status) {
    document.getElementById('retroStatus').textContent = status;
}

/**
 * Create audio element for remote audio
 */
function createAudioElement(id) {
    let audio = document.getElementById(id);
    if (!audio) {
        audio = document.createElement('audio');
        audio.id = id;
        audio.autoplay = true;
        document.body.appendChild(audio);
    }
    return audio;
}

/**
 * Play sound (placeholder - implement with actual audio files)
 */
function playSound(soundType) {
    // TODO: Implement sound playback here
    // For now, just log to avoid 404 errors
    console.log('Play sound:', soundType);
    // Example implementation when files are ready:
    // const audio = new Audio(`/static/sounds/${soundType}.mp3`);
    // audio.play().catch(e => console.log('Audio play failed:', e));
}

function stopSound(soundType) {
    // TODO: Implement sound stop here
    console.log('Stop sound:', soundType);
}
