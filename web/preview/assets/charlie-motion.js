/**
 * Charlie Protocol: Next-Gen Motion, HTML5 Interactive Imagery & Procedural Audio Engine
 * Progressive enhancement: zero-script fallback remains 100% functional.
 */

(function() {
  'use strict';

  // Check user preference for reduced motion
  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

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
      return path.includes('/preview/') ? './assets/fire-ambience.wav' : './preview/assets/fire-ambience.wav';
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
      this.preloadBuffer();

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
        this.preloadBuffer();
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
  function initChimneyEmbers() {
    const furnace = document.querySelector('.furnace');
    if (!furnace || prefersReducedMotion) return;

    const canvas = document.createElement('canvas');
    canvas.className = 'ember-canvas';
    furnace.appendChild(canvas);
    const ctx = canvas.getContext('2d');

    let width, height;
    function resize() {
      const rect = furnace.getBoundingClientRect();
      width = canvas.width = rect.width;
      height = canvas.height = rect.height;
    }
    resize();
    window.addEventListener('resize', resize);

    const particles = [];
    const maxParticles = 60;
    let mouseX = -1000, mouseY = -1000;

    furnace.addEventListener('mousemove', (e) => {
      const rect = furnace.getBoundingClientRect();
      mouseX = e.clientX - rect.left;
      mouseY = e.clientY - rect.top;
    });
    furnace.addEventListener('mouseleave', () => {
      mouseX = -1000; mouseY = -1000;
    });

    class Ember {
      constructor() {
        this.reset();
      }

      reset() {
        // Flue aperture lip (top 35% of stack, centered)
        this.x = width * 0.48 + (Math.random() - 0.5) * (width * 0.22);
        this.y = height * 0.38 + (Math.random() - 0.5) * 10;
        this.vx = (Math.random() - 0.5) * 0.8;
        this.vy = -(1.2 + Math.random() * 1.8);
        this.size = 1.2 + Math.random() * 2.2;
        this.life = 1.0;
        this.decay = 0.008 + Math.random() * 0.014;
        this.temp = 1.0; // 1 = core gold, 0.5 = ember orange, 0 = red ash
        this.swaySpeed = 0.03 + Math.random() * 0.05;
        this.swayOffset = Math.random() * Math.PI * 2;
      }

      update() {
        this.life -= this.decay;
        this.temp = Math.max(0, this.life);
        
        // Buoyancy + gentle horizontal wind sway
        this.vy -= 0.015;
        this.vx += Math.sin(Date.now() * 0.002 * this.swaySpeed + this.swayOffset) * 0.04;

        // Mouse wake deflection
        const dx = this.x - mouseX;
        const dy = this.y - mouseY;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 60) {
          const force = (60 - dist) / 60;
          this.vx += (dx / dist) * force * 1.5;
          this.vy += (dy / dist) * force * 1.5;
        }

        this.x += this.vx;
        this.y += this.vy;

        if (this.life <= 0 || this.y < -10) {
          this.reset();
        }
      }

      draw(ctx) {
        ctx.save();
        ctx.globalAlpha = Math.min(1, this.life * 1.3);

        let color;
        if (this.temp > 0.75) {
          color = '#FFD84A'; // Incandescent gold
        } else if (this.temp > 0.45) {
          color = '#FF8A1F'; // Molten thermal orange
        } else if (this.temp > 0.25) {
          color = '#8FE13F'; // Brand radioactive green spark
        } else {
          color = '#FF3B1F'; // Cinder red
        }

        ctx.fillStyle = color;
        ctx.shadowColor = color;
        ctx.shadowBlur = 6;
        ctx.beginPath();
        ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }
    }

    for (let i = 0; i < maxParticles; i++) {
      const p = new Ember();
      p.life = Math.random(); // Stagger initial births
      particles.push(p);
    }

    let isVisible = true;
    const observer = new IntersectionObserver((entries) => {
      isVisible = entries[0].isIntersecting;
    });
    observer.observe(furnace);

    function loop() {
      if (isVisible) {
        ctx.clearRect(0, 0, width, height);
        for (let i = 0; i < particles.length; i++) {
          particles[i].update();
          particles[i].draw(ctx);
        }
      }
      requestAnimationFrame(loop);
    }
    requestAnimationFrame(loop);
  }

  // =========================================================================
  // 3. Charlie Slug Evanescent Slime Trail (Canvas 2D)
  // =========================================================================
  function initSlugSlimeTrail() {
    const track = document.querySelector('.scene-track');
    const walker = document.querySelector('.walker');
    if (!track || !walker || prefersReducedMotion) return;

    const canvas = document.createElement('canvas');
    canvas.className = 'slime-canvas';
    track.appendChild(canvas);
    const ctx = canvas.getContext('2d');

    function resize() {
      canvas.width = track.clientWidth;
      canvas.height = track.clientHeight;
    }
    resize();
    window.addEventListener('resize', resize);

    const points = [];
    let lastX = null;

    function loop() {
      const rect = track.getBoundingClientRect();
      const walkerRect = walker.getBoundingClientRect();
      const currentX = walkerRect.left - rect.left + walkerRect.width * 0.5;
      const currentY = canvas.height - 4;

      if (lastX !== null && Math.abs(currentX - lastX) > 2) {
        points.push({
          x: currentX,
          y: currentY,
          alpha: 0.65,
          width: 8 + Math.random() * 4
        });
      }
      lastX = currentX;

      // Evanescent alpha decay
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (let i = points.length - 1; i >= 0; i--) {
        const pt = points[i];
        pt.alpha *= 0.982; // Ephemeral evaporation

        if (pt.alpha < 0.01) {
          points.splice(i, 1);
          continue;
        }

        ctx.save();
        ctx.fillStyle = `rgba(143, 225, 63, ${pt.alpha * 0.35})`;
        ctx.shadowColor = '#8FE13F';
        ctx.shadowBlur = 4;
        ctx.fillRect(pt.x - pt.width * 0.5, pt.y - 1, pt.width, 2.5);
        ctx.restore();
      }

      requestAnimationFrame(loop);
    }
    requestAnimationFrame(loop);
  }

  // =========================================================================
  // 4. Interactive Flywheel Loop with Incineration Event
  // =========================================================================
  function initInteractiveFlywheel() {
    const flyStage = document.querySelector('.fly-stage');
    const orbit = document.querySelector('.fly-orbit');
    const rider = document.querySelector('.fly-rider');
    if (!flyStage || !orbit || !rider || prefersReducedMotion) return;

    let angle = 0;
    let velocity = 0.5; // Natural baseline speed
    let isDragging = false;
    let startX = 0, startAngle = 0;
    let lastTickAngle = 0;
    let lastBurnAngle = 0;

    function applyRotation() {
      orbit.style.animation = 'none';
      rider.style.animation = 'none';
      orbit.style.transform = `rotate(${angle}deg)`;
      rider.style.transform = `translate(-50%, -50%) rotate(${-angle}deg)`;
    }

    // Scrubbing via pointer drag
    flyStage.addEventListener('pointerdown', (e) => {
      isDragging = true;
      startX = e.clientX;
      startAngle = angle;
      velocity = 0;
      flyStage.setPointerCapture(e.pointerId);
    });

    flyStage.addEventListener('pointermove', (e) => {
      if (!isDragging) return;
      const delta = (e.clientX - startX) * 0.8;
      angle = startAngle + delta;
      applyRotation();

      // Ratchet clicks on quadrature ticks (every 90 deg)
      if (Math.abs(angle - lastTickAngle) >= 45) {
        AudioEngine.playRatchet(1.2);
        lastTickAngle = angle;
      }
    });

    const endDrag = () => {
      if (!isDragging) return;
      isDragging = false;
      velocity = 0.5; // Resume natural cruise
    };
    flyStage.addEventListener('pointerup', endDrag);
    flyStage.addEventListener('pointercancel', endDrag);

    // Continuous physics loop
    function update() {
      if (!isDragging) {
        angle += velocity;
        applyRotation();
      }

      // Check for 6 o'clock incineration transit (180 deg mod 360)
      const normAngle = ((angle % 360) + 360) % 360;
      if (Math.abs(normAngle - 180) < 3 && Math.abs(angle - lastBurnAngle) > 20) {
        lastBurnAngle = angle;
        AudioEngine.playWhoosh();
        triggerFurnaceApertureFlash();
      }

      requestAnimationFrame(update);
    }
    requestAnimationFrame(update);

    function triggerFurnaceApertureFlash() {
      const hub = flyStage.querySelector('.fly-hub');
      if (!hub) return;
      hub.style.transition = 'filter 0.08s ease';
      hub.style.filter = 'drop-shadow(0 0 24px #FFD84A) brightness(1.4)';
      setTimeout(() => {
        hub.style.filter = 'none';
      }, 200);
    }
  }

  // =========================================================================
  // 5. Smart Clipboard Helper for Verification
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

    hint.textContent = 'Valid Base58 Solana public key format.';
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
  let stageBurstEmitter = null;

  function initHeroStageAnimation() {
    const stage = document.getElementById('heroScene3d');
    const canvas = document.getElementById('stageEmberCanvas');
    if (!stage || !canvas || prefersReducedMotion) return;

    const ctx = canvas.getContext('2d');
    let width = 0, height = 0;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    function resize() {
      const rect = stage.getBoundingClientRect();
      width = rect.width;
      height = rect.height;
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    resize();
    window.addEventListener('resize', resize);

    // Smoke & Ember arrays (Strictly confined to upper chimney sky, never over Charlie or furnace)
    const smokeParticles = [];
    const embers = [];
    const burstParticles = [];

    const maxSmoke = 20;
    const maxEmbers = 25;
    let mouseX = -1000, mouseY = -1000;

    stage.addEventListener('mousemove', (e) => {
      const rect = stage.getBoundingClientRect();
      mouseX = e.clientX - rect.left;
      mouseY = e.clientY - rect.top;
    });

    stage.addEventListener('mouseleave', () => {
      mouseX = -1000;
      mouseY = -1000;
    });

    // -------------------------------------------------------------
    // A. Volumetric Ambient Smoke Puffs from 3 Chimneys
    // -------------------------------------------------------------
    const getChimneys = () => [
      { x: width * 0.742, y: height * 0.175 },
      { x: width * 0.792, y: height * 0.155 },
      { x: width * 0.842, y: height * 0.155 }
    ];

    class SmokePuff {
      constructor(initial = false) {
        this.reset(initial);
      }

      reset(initial = false) {
        const chims = getChimneys();
        const chim = chims[Math.floor(Math.random() * chims.length)];
        this.x = chim.x + (Math.random() - 0.5) * 6;
        this.y = chim.y;
        this.vx = -(0.35 + Math.random() * 0.45); // wind drift leftwards
        this.vy = -(0.6 + Math.random() * 0.8);   // rising buoyancy
        this.size = 5 + Math.random() * 4;
        this.maxSize = 22 + Math.random() * 12;
        this.life = initial ? Math.random() : 1.0;
        this.decay = 0.006 + Math.random() * 0.007;
        this.rot = Math.random() * Math.PI * 2;
        this.vRot = (Math.random() - 0.5) * 0.02;
        this.seed = Math.random() * 100;
      }

      update() {
        this.life -= this.decay;
        const progress = 1.0 - Math.max(0, this.life);

        this.size = 6 + progress * (this.maxSize - 6);
        this.rot += this.vRot;
        this.x += this.vx + Math.sin(this.seed + progress * 4) * 0.3;
        this.y += this.vy;

        const dx = this.x - mouseX;
        const dy = this.y - mouseY;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 60) {
          const force = (60 - dist) / 60;
          this.vx += (dx / dist) * force * 1.0;
          this.vy += (dy / dist) * force * 1.0;
        }

        if (this.life <= 0 || this.y < -30 || this.x < -40) {
          this.reset();
        }
      }

      draw(ctx) {
        if (this.life <= 0) return;
        ctx.save();
        const progress = 1.0 - Math.max(0, this.life);
        
        let alpha = Math.max(0, 0.15 * Math.pow((1.0 - progress) / 0.85, 1.4));
        ctx.globalAlpha = alpha;
        
        const grad = ctx.createRadialGradient(this.x, this.y, 0, this.x, this.y, this.size);
        grad.addColorStop(0, 'rgba(45, 48, 55, 0.35)');
        grad.addColorStop(0.7, 'rgba(30, 32, 38, 0.18)');
        grad.addColorStop(1, 'rgba(15, 16, 20, 0)');

        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }
    }

    // -------------------------------------------------------------
    // B. Subtle Chimney Sparks High in the Night Sky
    // -------------------------------------------------------------
    class StageEmber {
      constructor() {
        this.reset(true);
      }

      reset(initial = false) {
        const chims = getChimneys();
        const chim = chims[Math.floor(Math.random() * chims.length)];
        this.x = chim.x + (Math.random() - 0.5) * 14;
        this.y = chim.y;
        this.vx = -(0.25 + Math.random() * 0.55);
        this.vy = -(1.0 + Math.random() * 1.6);
        this.decay = 0.008 + Math.random() * 0.012;
        this.size = 0.8 + Math.random() * 1.2;
        this.life = initial ? Math.random() : 1.0;
        this.temp = this.life;
        this.swaySpeed = 0.02 + Math.random() * 0.04;
        this.swayOffset = Math.random() * Math.PI * 2;
        this.isGreen = Math.random() < 0.25;
      }

      update() {
        this.life -= this.decay;
        this.temp = Math.max(0, this.life);
        this.vy -= 0.012;
        this.vx += Math.sin(Date.now() * 0.002 * this.swaySpeed + this.swayOffset) * 0.03;

        const dx = this.x - mouseX;
        const dy = this.y - mouseY;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 70) {
          const force = (70 - dist) / 70;
          this.vx += (dx / dist) * force * 1.5;
          this.vy += (dy / dist) * force * 1.5;
        }

        this.x += this.vx;
        this.y += this.vy;

        if (this.life <= 0 || this.y < -15 || this.x < -20) {
          this.reset();
        }
      }

      draw(ctx) {
        ctx.save();
        ctx.globalAlpha = Math.min(0.85, this.life * 1.2);
        const color = this.isGreen ? '#8FE13F' : (this.temp > 0.65 ? '#FFD84A' : (this.temp > 0.35 ? '#FF8A1F' : '#FF381E'));
        ctx.fillStyle = color;
        ctx.shadowColor = color;
        ctx.shadowBlur = this.isGreen ? 6 : 4;
        ctx.beginPath();
        ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }
    }

    class BurstSpark {
      constructor(x, y, isGreen = true) {
        this.x = x;
        this.y = y;
        const angle = Math.random() * Math.PI * 2;
        const speed = 2.5 + Math.random() * 5.0;
        this.vx = Math.cos(angle) * speed;
        this.vy = Math.sin(angle) * speed - 1.5;
        this.size = 1.8 + Math.random() * 2.4;
        this.life = 1.0;
        this.decay = 0.02 + Math.random() * 0.03;
        this.gravity = 0.12;
        this.isGreen = isGreen;
      }

      update() {
        this.life -= this.decay;
        this.vy += this.gravity;
        this.vx *= 0.98;
        this.x += this.vx;
        this.y += this.vy;
      }

      draw(ctx) {
        if (this.life <= 0) return;
        ctx.save();
        ctx.globalAlpha = Math.max(0, this.life);
        const color = this.isGreen 
          ? (this.life > 0.5 ? '#8FE13F' : '#FFD84A')
          : (this.life > 0.5 ? '#FF8A1F' : '#FF381E');
        ctx.fillStyle = color;
        ctx.shadowColor = color;
        ctx.shadowBlur = 10;
        ctx.beginPath();
        ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }
    }

    // Populate initial particle sets
    for (let i = 0; i < maxSmoke; i++) smokeParticles.push(new SmokePuff(true));
    for (let i = 0; i < maxEmbers; i++) embers.push(new StageEmber());

    stageBurstEmitter = function(x, y, count = 25, isGreen = true) {
      for (let i = 0; i < count; i++) {
        burstParticles.push(new BurstSpark(x, y, isGreen));
      }
    };

    function renderLoop() {
      ctx.clearRect(0, 0, width, height);

      // 1. Draw subtle chimney smoke in the sky
      for (let i = 0; i < smokeParticles.length; i++) {
        smokeParticles[i].update();
        smokeParticles[i].draw(ctx);
      }

      // 2. Draw subtle chimney sparks high in the sky
      for (let i = 0; i < embers.length; i++) {
        embers[i].update();
        embers[i].draw(ctx);
      }

      requestAnimationFrame(renderLoop);
    }
    renderLoop();
  }

  window.pokeCharlie = function() {};

  // =========================================================================
  // 8. Card & Table Interaction (Stable & Stationary)
  // =========================================================================
  function init3DCardTilt() {
    // Intentionally disabled: all cards and tables remain crisp, flat, and stationary when hovered.
  }

  // =========================================================================
  // 9. Harmonic Spring Number Counter Roll-Up Engine
  // =========================================================================
  function initSpringCounters() {
    const counterElements = document.querySelectorAll('.spring-counter, [data-counter-target]');
    if (!counterElements.length) return;

    const observer = new IntersectionObserver((entries, obs) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          const el = entry.target;
          obs.unobserve(el);
          animateCounter(el);
        }
      });
    }, { threshold: 0.25 });

    counterElements.forEach(el => observer.observe(el));

    function animateCounter(el) {
      const targetVal = parseFloat(el.getAttribute('data-counter-target') || el.textContent.replace(/[^0-9.-]+/g, ''));
      if (isNaN(targetVal)) return;

      const prefix = el.getAttribute('data-prefix') || '';
      const suffix = el.getAttribute('data-suffix') || '';
      const decimals = parseInt(el.getAttribute('data-decimals') || (targetVal % 1 !== 0 ? '6' : '0'), 10);
      const duration = parseInt(el.getAttribute('data-duration') || '1800', 10);

      const startTime = performance.now();
      let lastAudioTick = 0;

      function frame(now) {
        const elapsed = now - startTime;
        const progress = Math.min(1, elapsed / duration);
        // Harmonic out-exponential curve
        const ease = progress === 1 ? 1 : 1 - Math.pow(2, -10 * progress);
        const currentVal = targetVal * ease;

        if (decimals > 0) {
          el.textContent = `${prefix}${currentVal.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}${suffix}`;
        } else {
          el.textContent = `${prefix}${Math.round(currentVal).toLocaleString('en-US')}${suffix}`;
        }

        // Soft ratchet tick every ~80ms while rolling
        if (now - lastAudioTick > 90 && progress < 0.9) {
          lastAudioTick = now;
          AudioEngine.playRatchet(0.4);
        }

        if (progress < 1) {
          requestAnimationFrame(frame);
        } else {
          // Final crisp tick
          AudioEngine.playRatchet(0.6);
        }
      }

      requestAnimationFrame(frame);
    }
  }

  // =========================================================================
  // 10. Global Command Bar Hotkey & Quick-Action Search
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
  function initTelemetryStrip() {
    const elSlot = document.getElementById('telSlot');
    const elTps = document.getElementById('telTps');
    const elIncinerated = document.getElementById('telIncinerated');

    let currentSlot = 312894204;
    let baseIncinerated = 1489204.38;

    if (elSlot) {
      setInterval(() => {
        currentSlot += Math.floor(Math.random() * 2) + 1;
        elSlot.textContent = currentSlot.toLocaleString('en-US');
      }, 820);
    }

    if (elTps) {
      setInterval(() => {
        const tps = 2940 + Math.floor(Math.random() * 190) - 95;
        elTps.textContent = tps.toLocaleString('en-US');
      }, 1600);
    }

    if (elIncinerated) {
      setInterval(() => {
        baseIncinerated += 0.005 + Math.random() * 0.015;
        elIncinerated.textContent = `${baseIncinerated.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} SOL`;
      }, 4200);
    }
  }

  // =========================================================================
  // 12. Interactive Popperian Proof Explorer Sandbox Engine
  // =========================================================================
  window.toggleSandboxInvariant = function(key) {
    const btn = document.getElementById(`sandboxToggle_${key}`);
    if (!btn) return;

    const isActive = btn.classList.contains('active');
    if (isActive) {
      btn.classList.remove('active');
      btn.setAttribute('aria-pressed', 'false');
    } else {
      btn.classList.add('active');
      btn.setAttribute('aria-pressed', 'true');
    }

    AudioEngine.playClick();
    updateSandboxSimulation();
  };

  function updateSandboxSimulation() {
    const btnBps = document.getElementById('sandboxToggle_bps');
    const btnDev = document.getElementById('sandboxToggle_dev');
    const btnMint = document.getElementById('sandboxToggle_mint');
    const btnSig = document.getElementById('sandboxToggle_sig');

    const passBps = btnBps && btnBps.classList.contains('active');
    const passDev = btnDev && btnDev.classList.contains('active');
    const passMint = btnMint && btnMint.classList.contains('active');
    const passSig = btnSig && btnSig.classList.contains('active');

    const badge = document.getElementById('sandboxStatusBadge');
    const title = document.getElementById('sandboxStatusTitle');
    const message = document.getElementById('sandboxStatusMessage');
    const codeDiff = document.getElementById('sandboxCodeDiff');
    const card = document.getElementById('sandboxCard');

    const allPassed = passBps && passDev && passMint && passSig;

    if (allPassed) {
      if (badge) {
        badge.className = 'status-pill status-pill--verified';
        badge.innerHTML = '<span class="status-pill-led"></span><span class="status-pill-text">ALL INVARIANTS SATISFIED</span>';
      }
      if (title) {
        title.textContent = 'VERIFIED DEFLATIONARY // PROTOCOL PASS';
        title.style.color = 'var(--neon-charlie)';
      }
      if (message) {
        message.textContent = 'Every byte of creator fee distribution matches Solana Merkle ledger proofs. Invariant BPS sum is exactly 10,000. Dev rent extraction is 0. Token mint authority is revoked.';
      }
      if (codeDiff) {
        codeDiff.innerHTML = `<span style="color: var(--neon-charlie);">// Falsification Audit Report: OK</span>
EXPECTED: sum(bps) == 10000 && dev_bps == 0 && mint_auth == null
OBSERVED: sum(bps) = 10000 | dev_bps = 0 | mint_auth = null
STATUS: 0 BPS DISCREPANCY. ZERO RESIDUAL LEAKAGE.`;
      }
      if (card) {
        card.style.borderColor = 'rgba(143, 225, 63, 0.4)';
        card.style.boxShadow = '0 0 30px rgba(143, 225, 63, 0.12)';
      }
      AudioEngine.playConfirmationHum();
    } else {
      // Find the first failure
      let violatedName = '';
      let violatedDesc = '';
      let diffText = '';

      if (!passBps) {
        violatedName = 'INVARIANT // SPLIT_SUM_10000';
        violatedDesc = 'Total fee splits do not sum to 10,000 bps (100.00%). Residual fee units would leak to undeclared wallets.';
        diffText = `<span style="color: var(--flame-core);">// Falsification Audit Report: REJECTED</span>
EXPECTED: sum(bps) == 10000
OBSERVED: sum(bps) = 8500 (1500 BPS UNACCOUNTED)
STATUS: FALSIFIED. Protocol refused verification.`;
      } else if (!passDev) {
        violatedName = 'INVARIANT // DEV_RENT_EXTRACTION_ZERO';
        violatedDesc = 'Dev extraction wallet allocated > 0 BPS. The token promises deflation but routes protocol rent privately.';
        diffText = `<span style="color: var(--flame-core);">// Falsification Audit Report: REJECTED</span>
EXPECTED: dev_bps == 0
OBSERVED: dev_bps = 2500 (25.00% Private Extract)
STATUS: FALSIFIED. Rent seeking detected.`;
      } else if (!passMint) {
        violatedName = 'INVARIANT // MINT_AUTHORITY_REVOKED';
        violatedDesc = 'Mint authority is not null. Deployer or multi-sig can inflate supply at will, diluting incinerated burns.';
        diffText = `<span style="color: var(--flame-core);">// Falsification Audit Report: REJECTED</span>
EXPECTED: getMint(mint).mintAuthority == null
OBSERVED: mintAuthority = 7xKpkV9zN8uPgQw4xK1mReT8...
STATUS: FALSIFIED. Re-inflation vulnerability exists.`;
      } else {
        violatedName = 'INVARIANT // SIGNATURE_CRYPTOGRAPHIC_PROOF';
        violatedDesc = 'On-chain transaction signature failed Solana cryptographic Merkle verification. Transaction may be forged.';
        diffText = `<span style="color: var(--flame-core);">// Falsification Audit Report: REJECTED</span>
EXPECTED: ed25519_verify(tx_sig, pubkey, slot) == OK
OBSERVED: INVALID_SIGNATURE_HASH
STATUS: FALSIFIED. Forged RPC payload rejected.`;
      }

      if (badge) {
        badge.className = 'status-pill status-pill--refused';
        badge.innerHTML = '<span class="status-pill-led"></span><span class="status-pill-text">CHECK FALSIFIED</span>';
      }
      if (title) {
        title.textContent = `${violatedName} // REFUSED`;
        title.style.color = 'var(--flame-core)';
      }
      if (message) {
        message.textContent = violatedDesc;
      }
      if (codeDiff) {
        codeDiff.innerHTML = diffText;
      }
      if (card) {
        card.style.borderColor = 'rgba(255, 77, 46, 0.5)';
        card.style.boxShadow = '0 0 35px rgba(255, 77, 46, 0.18)';
      }
      AudioEngine.playFailureBuzzer();
    }
  }

  // =========================================================================
  // DOMContentLoaded Registration
  // =========================================================================
  document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('mintInput');
    if (input) {
      input.addEventListener('input', () => validateBase58Input(input));
    }

    // Initialize all motion and interactive engines
    initChimneyEmbers();
    initSlugSlimeTrail();
    initInteractiveFlywheel();
    initHeroStageAnimation();
    init3DCardTilt();
    initSpringCounters();
    initCommandBar();
    initTelemetryStrip();

    // Initialize Audio Engine & preload buffer
    AudioEngine.init();
  });

})();
