/* Finite launch-page embers. CSS owns every meaningful resting state. */
(() => {
  'use strict';
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const coarse = matchMedia('(pointer: coarse)');
  const canvas = document.getElementById('launchEmbers');
  if (!canvas || reduced.matches || coarse.matches || !('IntersectionObserver' in window)) return;

  const context = canvas.getContext('2d');
  if (!context) return;
  const particles = Array.from({length: 28}, (_, index) => ({
    x: ((index * 37) % 101) / 100,
    y: ((index * 61) % 97) / 100,
    speed: 0.018 + (index % 7) * 0.004,
    drift: ((index % 5) - 2) * 0.003,
    size: 0.7 + (index % 4) * 0.45,
    alpha: 0.16 + (index % 6) * 0.055,
  }));
  let visible = false;
  let frame = 0;
  let last = 0;

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const scale = Math.min(devicePixelRatio || 1, 1.5);
    canvas.width = Math.max(1, Math.round(rect.width * scale));
    canvas.height = Math.max(1, Math.round(rect.height * scale));
    context.setTransform(scale, 0, 0, scale, 0, 0);
  }
  function stop() {
    cancelAnimationFrame(frame);
    frame = 0;
    context.clearRect(0, 0, canvas.width, canvas.height);
  }
  function draw(now) {
    frame = 0;
    if (!visible || document.hidden || reduced.matches || coarse.matches) return stop();
    if (now - last >= 33) {
      const rect = canvas.getBoundingClientRect();
      const elapsed = last ? Math.min((now - last) / 1000, 0.1) : 0;
      last = now;
      context.clearRect(0, 0, rect.width, rect.height);
      for (const particle of particles) {
        particle.y -= particle.speed * elapsed;
        particle.x += particle.drift * elapsed;
        if (particle.y < -0.04) particle.y = 1.04;
        if (particle.x < 0) particle.x = 1;
        if (particle.x > 1) particle.x = 0;
        context.beginPath();
        context.fillStyle = `rgba(255, 116, 31, ${particle.alpha})`;
        context.arc(particle.x * rect.width, particle.y * rect.height, particle.size, 0, Math.PI * 2);
        context.fill();
      }
    }
    frame = requestAnimationFrame(draw);
  }
  function run() {
    if (!frame && visible && !document.hidden) frame = requestAnimationFrame(draw);
  }

  new ResizeObserver(resize).observe(canvas);
  new IntersectionObserver(entries => {
    visible = entries[0].isIntersecting;
    if (visible) run(); else stop();
  }, {threshold: 0.05}).observe(canvas);
  document.addEventListener('visibilitychange', () => document.hidden ? stop() : run());
  reduced.addEventListener('change', () => reduced.matches ? stop() : run());
  coarse.addEventListener('change', () => coarse.matches ? stop() : run());
  resize();
})();
