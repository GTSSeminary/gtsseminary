/* ============================================================
   GTS Hero — WebGL halftone dot-matrix particle system
   ------------------------------------------------------------
   Samples assets/images/hands.jpg (two hands reaching toward
   each other) down to a spatial grid of brightness values,
   uploads particle positions to a GPU vertex buffer and renders
   thousands of dynamic gl.POINTS. The size of each dot is
   computed in the vertex shader from the sampled pixel darkness
   (darker = larger dots, lighter = smaller / hidden), giving a
   halftone reading of the image. Interaction: u_mouse is sent
   to the vertex shader, which pushes nearby particles gently
   away with smooth damping back toward their origin.

   Vanilla JS, no frameworks or libraries.
   ============================================================ */
(function () {
  'use strict';

  var canvas = document.getElementById('heroCanvas');
  if (!canvas) return;

  // Static fallback content is fine for reduced-motion and small screens.
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if (window.innerWidth < 768) return;

  var gl;
  try {
    gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true }) ||
         canvas.getContext('experimental-webgl');
  } catch (e) { gl = null; }
  if (!gl) return;

  /* ---------- tunables ---------- */
  var SRC = 'assets/images/hands.jpg';
  var GRID_X = 190;         // sample density along the wide axis
  var LUM_MAX = 0.38;       // pixels brighter than this become void (hidden
  var PUSH_RADIUS = 150;    // css px influence radius around pointer
  var PUSH_FACTOR = 0.55;   // fraction of radius particles are pushed

  var dpr = Math.min(window.devicePixelRatio || 1, 2); // crisp on retina
  var cssW = 0;
  var cssH = 0;

  function readSize() {
    var rect = canvas.getBoundingClientRect();
    cssW = Math.max(1, Math.round(rect.width));
    cssH = Math.max(1, Math.round(rect.height));
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    gl.viewport(0, 0, canvas.width, canvas.height);
  }

  /* ---------- shaders ---------- */
  var VS_SRC = [
    'precision mediump float;',
    'attribute vec2  a_pos;',
    'attribute float a_dark;',
    'attribute float a_seed;',
    'uniform vec2  u_res;',
    'uniform vec2  u_mouse;',
    'uniform float u_radius;',
    'uniform float u_strength;',
    'uniform float u_dpr;',
    'uniform float u_time;',
    'varying float v_dark;',
    'void main() {',
    '  vec2 p = a_pos;',
    '  vec2 toMouse = p - u_mouse;',
    '  float d2 = dot(toMouse, toMouse);',
    '  float r2 = u_radius * u_radius;',
    '  float f = u_strength * exp(-d2 / r2);',
    '  if (d2 > 0.001) {',
    '    p += normalize(toMouse) * (f * u_radius * 0.55);',
    '  }',
    '  vec2 ndc = vec2(p.x * 2.0 - 1.0, 1.0 - p.y * 2.0);',
    '  gl_Position = vec4(ndc, 0.0, 1.0);',
    '  float d = clamp(a_dark, 0.0, 1.0);',
    '  float tw = 0.8 + 0.2 * sin(u_time * (1.4 + a_seed) + a_seed * 30.0);',
    '  float dsize = clamp(u_res.y / 42.0, 2.0, 7.0);',
    '  float sz = d * dsize * tw;',
    '  gl_PointSize = sz * u_dpr;',
    '  v_dark = d;',
    '}'
  ].join('\n');

  var FS_SRC = [
    'precision mediump float;',
    'varying float v_dark;',
    'void main() {',
    '  vec2 c = gl_PointCoord - 0.5;',
    '  float rr = dot(c, c);',
    '  if (rr > 0.25) { discard; }',
    '  float edge = smoothstep(0.25, 0.12, rr);',
    '  vec3 goldA = vec3(0.780, 0.620, 0.260);',
    '  vec3 goldB = vec3(0.960, 0.860, 0.620);',
    '  vec3 col = mix(goldA, goldB, clamp(v_dark, 0.0, 1.0));',
    '  gl_FragColor = vec4(col, 0.9 * edge);',
    '}'
  ].join('\n');

  /* ---------- program ---------- */
  var prog = null;
  var loc = {};

  function makeShader(type, src) {
    var sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) return null;
    return sh;
  }

  function buildProgram() {
    var vs = makeShader(gl.VERTEX_SHADER, VS_SRC);
    var fs = makeShader(gl.FRAGMENT_SHADER, FS_SRC);
    if (!vs || !fs) return false;
    var p = gl.createProgram();
    gl.attachShader(p, vs);
    gl.attachShader(p, fs);
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) return false;
    prog = p;
    loc.a_pos = gl.getAttribLocation(prog, 'a_pos');
    loc.a_dark = gl.getAttribLocation(prog, 'a_dark');
    loc.a_seed = gl.getAttribLocation(prog, 'a_seed');
    loc.u_res = gl.getUniformLocation(prog, 'u_res');
    loc.u_mouse = gl.getUniformLocation(prog, 'u_mouse');
    loc.u_radius = gl.getUniformLocation(prog, 'u_radius');
    loc.u_strength = gl.getUniformLocation(prog, 'u_strength');
    loc.u_dpr = gl.getUniformLocation(prog, 'u_dpr');
    loc.u_time = gl.getUniformLocation(prog, 'u_time');
    return true;
  }

  /* ---------- image sampler ---------- */
  var count = 0;
  var vbo = null;

  function luminance(r, g, b) { return 0.2126 * r + 0.7152 * g + 0.0722 * b; }

  function buildSampler(img) {
    var rows = Math.max(16, Math.round(GRID_X * (cssH / cssW)));
    var sw = GRID_X;
    var sh = rows;

    var off = document.createElement('canvas');
    off.width = sw;
    off.height = sh;
    var octx = off.getContext('2d');

    var iw = img.naturalWidth || 1600;
    var ih = img.naturalHeight || 1000;
    var scale = Math.max(sw / iw, sh / ih);
    var dw = iw * scale;
    var dh = ih * scale;
    var dx = (sw - dw) / 2;
    var dy = (sh - dh) / 2;
    octx.drawImage(img, dx, dy, dw, dh);

    var data;
    try { data = octx.getImageData(0, 0, sw, sh).data; }
    catch (e) { return false; }

    var px = [];
    var x, y, i, lum, darkness, seed;
    for (y = 0; y < sh; y++) {
      for (x = 0; x < sw; x++) {
        i = (y * sw + x) * 4;
        lum = luminance(data[i] / 255, data[i + 1] / 255, data[i + 2] / 255);
        if (lum > LUM_MAX) continue;            // bright hands -> void (hidden)
        darkness = LUM_MAX > 0 ? Math.min(1, (LUM_MAX - lum) / LUM_MAX) : 0;
        if (darkness < 0.04) continue;          // near-white noise
        px.push(x / sw, y / sh);
        px.push(darkness);
        px.push(((x * 73856093 ^ y * 19349663) >>> 0) / 4294967296);
      }
    }
    count = Math.floor(px.length / 4);
    if (count === 0) return false;

    var f32 = new Float32Array(count * 4);
    for (i = 0; i < count; i++) {
      f32[i * 4] = px[i * 4];
      f32[i * 4 + 1] = px[i * 4 + 1];
      f32[i * 4 + 2] = px[i * 4 + 2];
      f32[i * 4 + 3] = px[i * 4 + 3];
    }
    vbo = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
    gl.bufferData(gl.ARRAY_BUFFER, f32, gl.STATIC_DRAW);
    return true;
  }

  /* ---------- render loop ---------- */
  var mouseX = -9999;
  var mouseY = -9999;
  var strength = 0;
  var strengthTarget = 0;
  var running = false;
  var rafId = 0;
  var t0 = performance.now();

  function onMove(e) {
    var rect = canvas.getBoundingClientRect();
    mouseX = e.clientX - rect.left;
    mouseY = e.clientY - rect.top;
    strengthTarget = 1;
  }
  function onLeave() { strengthTarget = 0; }

  function damp() {
    strength += (strengthTarget - strength) * 0.06;
    if (strengthTarget === 0 && strength < 0.001) strength = 0;
  }

  function frame() {
    if (!running) return;
    var t = (performance.now() - t0) / 1000;
    damp();

    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    gl.useProgram(prog);
    gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
    gl.enableVertexAttribArray(loc.a_pos);
    gl.vertexAttribPointer(loc.a_pos, 2, gl.FLOAT, false, 16, 0);
    gl.enableVertexAttribArray(loc.a_dark);
    gl.vertexAttribPointer(loc.a_dark, 1, gl.FLOAT, false, 16, 8);
    gl.enableVertexAttribArray(loc.a_seed);
    gl.vertexAttribPointer(loc.a_seed, 1, gl.FLOAT, false, 16, 12);

    gl.uniform2f(loc.u_res, cssW, cssH);
    gl.uniform2f(loc.u_mouse, mouseX / cssW, mouseY / cssH);
    gl.uniform1f(loc.u_radius, PUSH_RADIUS / cssW);
    gl.uniform1f(loc.u_strength, strength);
    gl.uniform1f(loc.u_dpr, dpr);
    gl.uniform1f(loc.u_time, t);

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.drawArrays(gl.POINTS, 0, count);

    rafId = requestAnimationFrame(frame);
  }

  /* ---------- boot ---------- */
  function boot() {
    readSize();
    if (!buildProgram()) return;

    var img = new Image();
    img.onload = function () {
      if (!buildSampler(img)) return;
      document.documentElement.classList.add('px-on');
      running = true;
      requestAnimationFrame(frame);
    };
    img.onerror = function () { return; };
    img.src = SRC;

    window.addEventListener('mousemove', onMove, { passive: true });
    window.addEventListener('pointermove', onMove, { passive: true });
    window.addEventListener('mouseout', onLeave, { passive: true });
    window.addEventListener('blur', onLeave, { passive: true });

    var resizeTimer = 0;
    window.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        readSize();
      }, 150);
    }, { passive: true });
  }

  boot();
})();