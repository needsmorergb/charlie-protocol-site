/* Native, finite enhancements. The saved figures never advance with a clock. */
(() => {
  'use strict';
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const lowPower = matchMedia('(pointer: coarse)').matches || (navigator.hardwareConcurrency || 8) <= 4;
  if (reduced.matches || lowPower || !('IntersectionObserver' in window)) return;

  document.documentElement.classList.add('motion-capable');
  const reveals = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const el = entry.target;
      el.classList.add('revealed');
      reveals.unobserve(el);
      el.addEventListener('animationend', () => el.classList.remove('reveal-ready'), {once: true});
    }
  }, {threshold: 0.08});
  document.querySelectorAll('main > section, .counter-card, .fly-step-card, .dossier-card').forEach((el, i) => {
    el.style.setProperty('--i', i % 5);
    el.classList.add('reveal-ready');
    reveals.observe(el);
  });
  // A write-only, coalesced scroll update: no forced layout and no easing loop.
  let scrollFrame = 0;
  function scroll() {
    if (document.hidden || reduced.matches || scrollFrame) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = 0;
      document.body.style.setProperty('--hearth-y', `${Math.max(12, 72 - window.scrollY * 0.035)}%`);
      energize();
    });
  }
  window.addEventListener('scroll', scroll, {passive: true});

  const canvas = document.getElementById('furnaceCanvas');
  let energize = () => {};
  if (!canvas) return;
  let gl, program, buffer, timeUniform, heatUniform;
  let visible = false, frame = 0, until = 0, lastFrame = 0, lost = false;
  function stop() { cancelAnimationFrame(frame); frame = 0; canvas.style.opacity = '0'; }
  function init() {
    try {
      gl = canvas.getContext('webgl', {alpha: true, premultipliedAlpha: false, antialias: false});
      if (!gl) return false;
      function shader(type, source) {
        const shader = gl.createShader(type);
        gl.shaderSource(shader, source); gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) { gl.deleteShader(shader); throw Error('Shader unavailable'); }
        return shader;
      }
      const vertex = shader(gl.VERTEX_SHADER, 'attribute vec2 p; varying vec2 uv; void main(){uv=p*.5+.5;gl_Position=vec4(p,0.,1.);}');
      const fragment = shader(gl.FRAGMENT_SHADER, `precision mediump float;
        varying vec2 uv; uniform float time; uniform float heat;
        float hash(float n){return fract(sin(n*127.1)*43758.5453);}
        void main(){
          vec2 hearth=vec2(.76,.36);
          float glow=exp(-length((uv-hearth)*vec2(3.,5.))*5.);
          float sparks=0.;
          for(int i=0;i<12;i++){
            float n=float(i); float age=fract(time*.22+hash(n));
            vec2 pos=hearth+vec2((hash(n+12.)-.5)*.24+sin(age*5.+n)*.018,age*.48);
            sparks+=smoothstep(.007,0.,length(uv-pos))*(1.-age);
          }
          float alpha=(glow*.28+sparks*.65)*heat;
          gl_FragColor=vec4(vec3(.95,.37,.075),alpha);
        }`);
      program = gl.createProgram(); gl.attachShader(program, vertex); gl.attachShader(program, fragment); gl.linkProgram(program);
      gl.deleteShader(vertex); gl.deleteShader(fragment);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw Error('Program unavailable');
      gl.useProgram(program);
      buffer = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,1,1]), gl.STATIC_DRAW);
      const position = gl.getAttribLocation(program, 'p');
      gl.enableVertexAttribArray(position); gl.vertexAttribPointer(position,2,gl.FLOAT,false,0,0);
      timeUniform = gl.getUniformLocation(program,'time'); heatUniform = gl.getUniformLocation(program,'heat');
      return true;
    } catch { stop(); return false; }
  }
  function draw(now) {
    frame = 0;
    if (!visible || document.hidden || reduced.matches || lost || now >= until) { stop(); return; }
    if (now - lastFrame >= 33) {
      lastFrame = now;
      gl.uniform1f(timeUniform, now / 1000);
      gl.uniform1f(heatUniform, Math.min(1, (until - now) / 500));
      gl.drawArrays(gl.TRIANGLE_STRIP,0,4);
    }
    frame = requestAnimationFrame(draw);
  }
  const visibility = new IntersectionObserver(entries => {
    visible = entries[0].isIntersecting;
    if (!visible) stop();
  });
  visibility.observe(canvas);
  if (!init()) return;
  new ResizeObserver(entries => {
    const {width, height} = entries[0].contentRect;
    canvas.width = Math.max(1, Math.round(width * Math.min(devicePixelRatio || 1, 1.5)));
    canvas.height = Math.max(1, Math.round(height * Math.min(devicePixelRatio || 1, 1.5)));
    if (!lost) gl.viewport(0,0,canvas.width,canvas.height);
  }).observe(canvas);
  energize = () => {
    if (!visible || document.hidden || reduced.matches || lost) return;
    until = performance.now() + 900;
    canvas.style.opacity = '1';
    if (!frame) frame = requestAnimationFrame(draw);
  };
  // Only scrolling the furnace into view adds heat. There is no ambient timer.
  canvas.addEventListener('webglcontextlost', event => { event.preventDefault(); lost = true; stop(); });
  canvas.addEventListener('webglcontextrestored', () => { lost = !init(); if (!lost) gl.viewport(0,0,canvas.width,canvas.height); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
  reduced.addEventListener('change', () => { if (reduced.matches) stop(); });
})();
