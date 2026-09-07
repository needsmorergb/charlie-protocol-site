/**
 * Charlie Protocol: Next-Gen Motion, HTML5 Interactive Imagery & Procedural Audio Engine
 * Progressive enhancement: zero-script fallback remains 100% functional.
 */

(function() {
  'use strict';

  // Check user preference for reduced motion
  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // =========================================================================
  // Telemetry & Traffic Analytics Dispatcher (Vercel Web Analytics)
  // =========================================================================
  window.CharlieAnalytics = {
    track: function(eventName, data) {
      if (typeof window.va === 'function') {
        try {
          window.va('track', eventName, data || {});
        } catch (e) {
          console.debug('Analytics dispatch skipped:', e);
        }
      }
    }
  };

  // =========================================================================
  // 1. Studio-Grade Seamless Web Audio Fire Ambience Engine (Zero Cutoff Loop)
  // =========================================================================
  const AudioEngine = {
    ctx: null,
    masterGain: null,
    sourceNode: null,
    audioBuffer: null,
    procSource: null,
    procGain: null,
    isLoading: false,
    isPlaying: false,
    muted: true,
    htmlAudioA: null,
    htmlAudioB: null,
    useHtmlFallback: false,

    getAudioUrl() {
      const path = window.location.pathname;
      return './assets/fire-ambience.wav';
    },

    getAudioContext() {
      if (!this.ctx) {
        const AudioCtxClass = window.AudioContext || window.webkitAudioContext;
        if (AudioCtxClass) {
          try {
            this.ctx = new AudioCtxClass();
            this.masterGain = this.ctx.createGain();
            this.masterGain.gain.setValueAtTime(0, this.ctx.currentTime);
            this.masterGain.connect(this.ctx.destination);
          } catch (e) {
            console.warn('AudioContext creation failed, using HTML fallback', e);
            this.useHtmlFallback = true;
          }
        } else {
          this.useHtmlFallback = true;
        }
      }
      return this.ctx;
    },

    init() {
      const saved = localStorage.getItem('charlie_preview_audio');
      this.muted = (saved !== 'true');
      this.updateButtonUI();

      // Preload the audio buffer in the background immediately
      if (!this.muted) this.preloadBuffer();

      // Ensure AudioContext is unlocked and resumed on first user gesture
      const unlockAudio = () => {
        if (this.ctx && this.ctx.state === 'suspended') {
          this.ctx.resume().then(() => {
            if (!this.muted && !this.isPlaying) {
              this.startFireAmbience();
            }
          });
        } else if (!this.muted && !this.isPlaying) {
          this.startFireAmbience();
        }
      };
      window.addEventListener('click', unlockAudio, { passive: true });
      window.addEventListener('keydown', unlockAudio, { passive: true });
      window.addEventListener('touchstart', unlockAudio, { passive: true });
    },

    async preloadBuffer() {
      if (this.audioBuffer || this.isLoading || this.useHtmlFallback) return;
      this.isLoading = true;
      try {
        const ctx = this.getAudioContext();
        if (!ctx) {
          this.initHtmlFallback();
          return;
        }
        const res = await fetch(this.getAudioUrl());
        const arrayBuf = await res.arrayBuffer();
        this.audioBuffer = await ctx.decodeAudioData(arrayBuf);
        this.isLoading = false;

        // If user activated audio while we were decoding, start immediately
        if (!this.muted && (!this.isPlaying || this.procSource)) {
          this.startFireAmbience();
        }
      } catch (e) {
        console.warn('Web Audio buffer fetch/decode failed, using HTML fallback:', e);
        this.isLoading = false;
        this.useHtmlFallback = true;
        this.initHtmlFallback();
      }
    },

    // Procedural warm flame rustle to play instantaneously while WAV buffer decodes
    startProceduralBed() {
      if (this.procSource) return;
      const ctx = this.getAudioContext();
      if (!ctx || ctx.state === 'suspended') return;

      try {
        const sampleRate = ctx.sampleRate || 44100;
        const bufLen = sampleRate * 3;
        const noiseBuf = ctx.createBuffer(1, bufLen, sampleRate);
        const data = noiseBuf.getChannelData(0);
        let b0 = 0, b1 = 0, b2 = 0;
        for (let i = 0; i < bufLen; i++) {
          const white = (Math.random() * 2 - 1);
          b0 = 0.992 * b0 + white * 0.05;
          b1 = 0.96 * b1 + white * 0.12;
          b2 = 0.86 * b2 + white * 0.22;
          data[i] = (b0 + b1 + b2) * 0.28;
        }

        this.procSource = ctx.createBufferSource();
        this.procSource.buffer = noiseBuf;
        this.procSource.loop = true;

        const filter = ctx.createBiquadFilter();
        filter.type = 'lowpass';
        filter.frequency.value = 900;

        this.procGain = ctx.createGain();
        this.procGain.gain.setValueAtTime(0.001, ctx.currentTime);
        this.procGain.gain.linearRampToValueAtTime(0.35, ctx.currentTime + 0.3);

        this.procSource.connect(filter);
        filter.connect(this.procGain);
        this.procGain.connect(this.masterGain);

        this.procSource.start(0);
      } catch (e) {}
    },

    stopProceduralBed() {
      if (this.procGain && this.ctx) {
        try {
          const now = this.ctx.currentTime;
          this.procGain.gain.cancelScheduledValues(now);
          this.procGain.gain.linearRampToValueAtTime(0.001, now + 0.2);
        } catch(e) {}
      }
      setTimeout(() => {
        if (this.procSource) {
          try {
            this.procSource.stop();
            this.procSource.disconnect();
          } catch(e) {}
          this.procSource = null;
          this.procGain = null;
        }
      }, 250);
    },

    initHtmlFallback() {
      if (this.htmlAudioA) return;
      const url = this.getAudioUrl();
      this.htmlAudioA = new Audio(url);
      this.htmlAudioA.preload = 'auto';
      this.htmlAudioB = new Audio(url);
      this.htmlAudioB.preload = 'auto';

      // Dual-deck ping-pong looping to eliminate the native loop boundary gap
      const setupDeckLoop = (deck, otherDeck) => {
        deck.addEventListener('timeupdate', () => {
          if (!this.muted && deck.duration && deck.currentTime > deck.duration - 1.5) {
            if (otherDeck.paused) {
              otherDeck.currentTime = 0;
              otherDeck.volume = 0.55;
              otherDeck.play().catch(() => {});
            }
          }
        });
      };
      setupDeckLoop(this.htmlAudioA, this.htmlAudioB);
      setupDeckLoop(this.htmlAudioB, this.htmlAudioA);
    },

    startHtmlFallback() {
      this.initHtmlFallback();
      if (!this.htmlAudioA) return;
      this.htmlAudioA.volume = 0.55;
      this.htmlAudioA.currentTime = 0;
      this.htmlAudioA.play().catch(() => {});
      this.isPlaying = true;
    },

    stopHtmlFallback() {
      if (this.htmlAudioA) {
        this.htmlAudioA.pause();
        this.htmlAudioA.currentTime = 0;
      }
      if (this.htmlAudioB) {
        this.htmlAudioB.pause();
        this.htmlAudioB.currentTime = 0;
      }
      this.isPlaying = false;
    },

    toggle() {
      this.muted = !this.muted;
      localStorage.setItem('charlie_preview_audio', !this.muted);
      this.updateButtonUI();

      if (window.CharlieAnalytics) {
        window.CharlieAnalytics.track('Audio Toggle', { state: this.muted ? 'off' : 'on' });
      }

      if (!this.muted) {
        this.startFireAmbience();
      } else {
        this.stopFireAmbience();
      }
    },

    updateButtonUI() {
      const btn = document.getElementById('audioToggle');
      const stageBtn = document.getElementById('btnStageAudio');
      if (btn) {
        if (this.muted) {
          btn.classList.remove('active');
          btn.innerHTML = '<span>🔇</span><span>Fire Off</span>';
        } else {
          btn.classList.add('active');
          btn.innerHTML = '<span>🔥</span><span>Fire On</span>';
        }
      }
      if (stageBtn) {
        if (this.muted) {
          stageBtn.classList.remove('active');
          stageBtn.innerHTML = '🔇 Fire Off';
        } else {
          stageBtn.classList.add('active');
          stageBtn.innerHTML = '🔥 Fire Rustling';
        }
      }
    },

    startFireAmbience() {
      if (this.muted) return;

      if (this.useHtmlFallback) {
        this.startHtmlFallback();
        return;
      }

      const ctx = this.getAudioContext();
      if (!ctx) {
        this.startHtmlFallback();
        return;
      }

      if (ctx.state === 'suspended') {
        ctx.resume();
      }

      // If buffer is still loading, start procedural flame bed in the interim
      if (!this.audioBuffer) {
        this.startProceduralBed();
        if (!this.muted) this.preloadBuffer();
        this.isPlaying = true;
        return;
      }

      // If already playing buffer source, just ensure volume is up
      if (this.isPlaying && this.sourceNode) {
        const now = ctx.currentTime;
        this.masterGain.gain.cancelScheduledValues(now);
        this.masterGain.gain.setValueAtTime(this.masterGain.gain.value || 0.001, now);
        this.masterGain.gain.linearRampToValueAtTime(0.55, now + 0.3);
        return;
      }

      // Create pristine looping buffer source in Web Audio (0ms gap hardware loop)
      try {
        if (this.sourceNode) {
          try { this.sourceNode.stop(); this.sourceNode.disconnect(); } catch(e) {}
        }
        this.sourceNode = ctx.createBufferSource();
        this.sourceNode.buffer = this.audioBuffer;
        this.sourceNode.loop = true;
        this.sourceNode.loopStart = 0;
        this.sourceNode.loopEnd = this.audioBuffer.duration;

        this.sourceNode.connect(this.masterGain);

        // Smooth fade in
        const now = ctx.currentTime;
        this.masterGain.gain.cancelScheduledValues(now);
        this.masterGain.gain.setValueAtTime(this.masterGain.gain.value || 0.001, now);
        this.masterGain.gain.linearRampToValueAtTime(0.55, now + 0.5);

        this.sourceNode.start(0);
        this.isPlaying = true;

        // Stop procedural bed if it was running
        this.stopProceduralBed();
      } catch (e) {
        console.warn('Web Audio playback error, using HTML fallback:', e);
        this.startHtmlFallback();
      }
    },

    stopFireAmbience() {
      this.stopProceduralBed();
      if (this.useHtmlFallback) {
        this.stopHtmlFallback();
        return;
      }

      if (this.ctx && this.masterGain) {
        const now = this.ctx.currentTime;
        this.masterGain.gain.cancelScheduledValues(now);
        this.masterGain.gain.setValueAtTime(this.masterGain.gain.value, now);
        this.masterGain.gain.linearRampToValueAtTime(0.0001, now + 0.35);

        setTimeout(() => {
          if (this.muted && this.sourceNode) {
            try {
              this.sourceNode.stop();
              this.sourceNode.disconnect();
            } catch(e) {}
            this.sourceNode = null;
            this.isPlaying = false;
          }
        }, 400);
      } else {
        this.stopHtmlFallback();
      }
    },

    // Silent no-ops so only the continuous fire rustles in the background without distraction
    playRatchet() {},
    playWhoosh() {},
    playSquish() {},
    playClick() {},
    playChirp() {},
    playConfirmationHum() {},
    playFailureBuzzer() {},
    playScanBlip() {},
    playRadarTick() {}
  };

  // Expose toggle & AudioEngine globally
  window.AudioEngine = AudioEngine;
  window.toggleAudio = function() {
    AudioEngine.toggle();
  };

  // Universal clipboard copy helper with visual and audible feedback
  window.copyToClipboard = function(text, element) {
    if (window.AudioEngine) window.AudioEngine.playChirp();
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => {
        showCopyFeedback(element);
      }).catch(() => {
        fallbackCopy(text, element);
      });
    } else {
      fallbackCopy(text, element);
    }
  };

  function fallbackCopy(text, element) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      showCopyFeedback(element);
    } catch(e) {}
    document.body.removeChild(ta);
  }

  function showCopyFeedback(element) {
    if (!element) return;
    const origHtml = element.innerHTML;
    element.classList.add('copied');
    element.innerHTML = '<span>✓ Copied!</span>';
    setTimeout(() => {
      element.classList.remove('copied');
      element.innerHTML = origHtml;
    }, 1600);
  }

  // =========================================================================
  // 2. Chimney Particle Embers (Canvas 2D Physics)
  // =========================================================================
  window.pasteCA = async function(inputId = 'mintInput') {
    const input = document.getElementById(inputId);
    if (!input) return;

    try {
      if (navigator.clipboard && navigator.clipboard.readText) {
        const text = await navigator.clipboard.readText();
        const trimmed = text.trim();
        if (trimmed) {
          input.value = trimmed;
          input.dispatchEvent(new Event('input', { bubbles: true }));
          AudioEngine.playRatchet(0.8);
          validateBase58Input(input);
        }
      } else {
        input.focus();
      }
    } catch (err) {
      console.warn('Clipboard read failed', err);
      input.focus();
    }
  };

  window.setExampleCA = function(mint, inputId = 'mintInput') {
    const input = document.getElementById(inputId);
    if (!input) return;
    input.value = mint;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    AudioEngine.playRatchet(0.8);
    validateBase58Input(input);
  };

  function validateBase58Input(input) {
    const val = input.value.trim();
    const hint = document.getElementById('inputHint');
    if (!hint) return;

    if (!val) {
      hint.textContent = 'Paste a pump.fun contract address (32-44 Base58 characters)';
      hint.style.color = 'var(--text-muted)';
      return;
    }

    const illegalChars = ['0', 'O', 'I', 'l'];
    for (let ch of illegalChars) {
      if (val.includes(ch)) {
        hint.textContent = `Warning: contains '${ch}' which is invalid in Solana Base58.`;
        hint.style.color = 'var(--fail)';
        return;
      }
    }

    if (val.length < 32 || val.length > 44) {
      hint.textContent = `Length: ${val.length} / 44 characters`;
      hint.style.color = 'var(--unchecked)';
      return;
    }

    hint.textContent = 'Address format looks right. Submit to check the full address.';
    hint.style.color = 'var(--pass)';
  }

  // =========================================================================
  // 6. 3D Parallax & Hero Mode Switcher
  // =========================================================================
  window.setHeroMode = function(mode) {
    const box3d = document.getElementById('heroScene3d');
    const boxClassic = document.getElementById('heroSceneClassic');
    const btn3d = document.getElementById('btnMode3d');
    const btnClassic = document.getElementById('btnModeClassic');

    if (!box3d || !boxClassic) return;

    if (mode === '3d') {
      box3d.style.display = 'block';
      boxClassic.style.display = 'none';
      if (btn3d) btn3d.classList.add('active');
      if (btnClassic) btnClassic.classList.remove('active');
    } else {
      box3d.style.display = 'none';
      boxClassic.style.display = 'block';
      if (btn3d) btn3d.classList.remove('active');
      if (btnClassic) btnClassic.classList.add('active');
    }

    AudioEngine.playRatchet(1.0);
  };


  window.handle3dParallax = function() {};
  window.reset3dParallax = function() {};

  // =========================================================================
  // 7. Hero 3D Stage Animation Engine (Flowing Fire, Volumetric Smoke & Embers)
  // =========================================================================
  function initCommandBar() {
    window.addEventListener('keydown', (e) => {
      // Hotkey: '/' or 'Cmd+K' / 'Ctrl+K'
      const isSearchKey = e.key === '/' || ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k');
      const activeTag = document.activeElement ? document.activeElement.tagName.toLowerCase() : '';
      const isEditing = activeTag === 'input' || activeTag === 'textarea';

      if (isSearchKey && !isEditing) {
        e.preventDefault();
        const searchInput = document.getElementById('commandSearchInput') || document.getElementById('mintInput') || document.getElementById('verifyInput');
        if (searchInput) {
          searchInput.focus();
          searchInput.select();
          AudioEngine.playClick();
          
          // Flash command box
          const container = searchInput.closest('.command-hero-box') || searchInput.closest('.verify-box');
          if (container) {
            container.classList.add('command-focused');
            setTimeout(() => container.classList.remove('command-focused'), 300);
          }
        }
      }
    });
  }

  // =========================================================================
  // 11. Live Facility Telemetry Strip Simulator
  // =========================================================================
  // =========================================================================
  // 12. Interactive Popperian Proof Explorer Sandbox Engine
  // =========================================================================
  // DOMContentLoaded Registration
  // =========================================================================
  document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('mintInput');
    if (input) {
      input.addEventListener('input', () => validateBase58Input(input));
    }

    // Initialize all motion and interactive engines
    initCommandBar();

    // Initialize Audio Engine & preload buffer
    AudioEngine.init();
  });

})();
