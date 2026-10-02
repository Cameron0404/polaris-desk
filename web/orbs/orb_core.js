/* orb_core.js - the shared runtime for the Polaris 3D orbs. Raw WebGL2, no library, no network.

   One state object drives every orb, the same one the server's /api/desk produces:
       {state: "idle" | "listening" | "working" | "speaking" | "error", agents: N, level: 0..1}
   `agents` is the count of running agents (working), `level` the audio level (speaking). An orb
   module is one file that calls PolarisOrbs.register(name, def); this file owns everything they
   share: the GL context, the frame loop, the frame cap, the pause rules, bloom, smoothing between
   states, the stats and the self-test.

   Cost when unused is nothing: the page loads this file and one orb file only when the Home screen
   shows a 3D orb. Cost when idle is capped: 30 frames a second at idle, 60 when something is
   happening, no frames at all when the window is hidden, the canvas is off screen or detached,
   and one still frame per change under prefers-reduced-motion or `motion: false`.

       const h = PolarisOrbs.mount(canvas, "neural", {bloom: true});   // null when there is no WebGL2
       h.set({state: "working", agents: 3});
       h.stats();  h.pause();  h.resume();  h.destroy();
       PolarisOrbs.selftest("neural")   // -> {ok, checks: [[name, ok, detail], ...]}
*/
(function (root) {
"use strict";

const STATES = ["idle", "listening", "working", "speaking", "error"];
const REG = Object.create(null);
// How lively each state is. Orbs read the smoothed value as uEnergy.
const ENERGY = {idle: 0.15, listening: 0.45, working: 0.8, speaking: 0.55, error: 0.5};

function register(name, def) { def.name = name; REG[name] = def; }

// ------------------------------------------------------------------ small maths
const M = {
  ident() { const m = new Float32Array(16); m[0] = m[5] = m[10] = m[15] = 1; return m; },
  perspective(fovy, aspect, near, far) {
    const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far), m = new Float32Array(16);
    m[0] = f / aspect; m[5] = f; m[10] = (far + near) * nf; m[11] = -1; m[14] = 2 * far * near * nf; return m;
  },
  lookAt(e, c, up) {
    let zx = e[0] - c[0], zy = e[1] - c[1], zz = e[2] - c[2], l = Math.hypot(zx, zy, zz); zx /= l; zy /= l; zz /= l;
    let xx = up[1] * zz - up[2] * zy, xy = up[2] * zx - up[0] * zz, xz = up[0] * zy - up[1] * zx; l = Math.hypot(xx, xy, xz); xx /= l; xy /= l; xz /= l;
    const yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx, m = new Float32Array(16);
    m[0] = xx; m[1] = yx; m[2] = zx; m[4] = xy; m[5] = yy; m[6] = zy; m[8] = xz; m[9] = yz; m[10] = zz;
    m[12] = -(xx * e[0] + xy * e[1] + xz * e[2]); m[13] = -(yx * e[0] + yy * e[1] + yz * e[2]); m[14] = -(zx * e[0] + zy * e[1] + zz * e[2]); m[15] = 1;
    return m;
  },
  mul(a, b) {   // a * b, column-major
    const o = new Float32Array(16);
    for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
      let s = 0; for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k]; o[c * 4 + r] = s;
    }
    return o;
  },
  rotY(a) { const m = M.ident(), c = Math.cos(a), s = Math.sin(a); m[0] = c; m[2] = -s; m[8] = s; m[10] = c; return m; },
  rotX(a) { const m = M.ident(), c = Math.cos(a), s = Math.sin(a); m[5] = c; m[6] = s; m[9] = -s; m[10] = c; return m; },
};

// A seeded generator, so every orb is the same orb on every load and in every screenshot.
function rng(seed) {
  let a = seed >>> 0;
  return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

// 3D simplex noise (Ashima Arts / Stefan Gustavson, MIT). The one noise every orb uses.
const NOISE = `
vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}
float snoise(vec3 v){
  const vec2 C=vec2(1.0/6.0,1.0/3.0); const vec4 D=vec4(0.0,0.5,1.0,2.0);
  vec3 i=floor(v+dot(v,C.yyy)); vec3 x0=v-i+dot(i,C.xxx);
  vec3 g=step(x0.yzx,x0.xyz); vec3 l=1.0-g; vec3 i1=min(g.xyz,l.zxy); vec3 i2=max(g.xyz,l.zxy);
  vec3 x1=x0-i1+C.xxx; vec3 x2=x0-i2+C.yyy; vec3 x3=x0-D.yyy;
  i=mod289(i);
  vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));
  float n_=0.142857142857; vec3 ns=n_*D.wyz-D.xzx;
  vec4 j=p-49.0*floor(p*ns.z*ns.z);
  vec4 x_=floor(j*ns.z); vec4 y_=floor(j-7.0*x_);
  vec4 x=x_*ns.x+ns.yyyy; vec4 y=y_*ns.x+ns.yyyy; vec4 h=1.0-abs(x)-abs(y);
  vec4 b0=vec4(x.xy,y.xy); vec4 b1=vec4(x.zw,y.zw);
  vec4 s0=floor(b0)*2.0+1.0; vec4 s1=floor(b1)*2.0+1.0; vec4 sh=-step(h,vec4(0.0));
  vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy; vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;
  vec3 p0=vec3(a0.xy,h.x); vec3 p1=vec3(a0.zw,h.y); vec3 p2=vec3(a1.xy,h.z); vec3 p3=vec3(a1.zw,h.w);
  vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
  p0*=norm.x;p1*=norm.y;p2*=norm.z;p3*=norm.w;
  vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0); m=m*m;
  return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
}
`;
// The uniforms every orb receives, declared once.
const COMMON = `
uniform float uTime, uEnergy, uLevel, uAgents, uPx;
// Phases: speed integrated over time. Anything whose speed follows the state turns by one of
// these, never by uTime * speed: that product re-scales every second already run the moment the
// speed changes, so a state change after a long run would jump the scene forward by many turns
// at once. uPhase = integral of uEnergy, uVPhase = of the voice
// (uMix.z * uLevel), uAgentPh[i] = of how lit agent i is (uMix.y * clamp(uAgents - i, 0, 1)).
uniform float uPhase, uVPhase;
uniform float uAgentPh[12];
uniform vec4 uMix;          // listening, working, speaking, error (idle is what is left)
uniform vec3 uColor, uColor2;
uniform vec2 uRes;
`;

// ------------------------------------------------------------------ GL helpers
function shader(gl, type, src) {
  const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    const e = gl.getShaderInfoLog(s); gl.deleteShader(s);
    throw new Error("shader: " + e);
  }
  return s;
}
function program(gl, vs, fs) {
  const head = "#version 300 es\nprecision highp float;\n";
  const p = gl.createProgram();
  gl.attachShader(p, shader(gl, gl.VERTEX_SHADER, head + vs));
  gl.attachShader(p, shader(gl, gl.FRAGMENT_SHADER, head + fs));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error("link: " + gl.getProgramInfoLog(p));
  const u = {}, n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
  for (let i = 0; i < n; i++) { const info = gl.getActiveUniform(p, i); u[info.name.replace(/\[0\]$/, "")] = gl.getUniformLocation(p, info.name); }
  return {p, u};
}
// Set uniforms by value shape: number -> float, [a,b(,c(,d))] -> vecN, Float32Array(16) -> mat4,
// any other Float32Array -> float[]. Names the program does not use are skipped.
function uni(gl, prog, vals) {
  for (const k in vals) {
    const loc = prog.u[k]; if (loc == null) continue;
    const v = vals[k];
    if (typeof v === "number") gl.uniform1f(loc, v);
    else if (v instanceof Float32Array && v.length === 16) gl.uniformMatrix4fv(loc, false, v);
    else if (v instanceof Float32Array) gl.uniform1fv(loc, v);
    else if (v.length === 2) gl.uniform2f(loc, v[0], v[1]);
    else if (v.length === 3) gl.uniform3f(loc, v[0], v[1], v[2]);
    else if (v.length === 4) gl.uniform4f(loc, v[0], v[1], v[2], v[3]);
  }
}
// A vertex array from [{data, size, loc}]: location N in the shader is attrs[i].loc.
function vao(gl, attrs) {
  const v = gl.createVertexArray(), bufs = []; gl.bindVertexArray(v);
  for (const a of attrs) {
    const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, a.data, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(a.loc); gl.vertexAttribPointer(a.loc, a.size, gl.FLOAT, false, 0, 0);
    bufs.push(b);
  }
  gl.bindVertexArray(null);
  return {v, bufs, free() { gl.deleteVertexArray(v); bufs.forEach(b => gl.deleteBuffer(b)); }};
}
// The full-screen triangle, for glows, plasma and the bloom passes. No buffer needed.
const FS_VS = `out vec2 vUv; void main(){ vec2 p=vec2((gl_VertexID<<1)&2, gl_VertexID&2); vUv=p; gl_Position=vec4(p*2.0-1.0,0.0,1.0); }`;

function target(gl, w, h, hdr) {
  const tex = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texImage2D(gl.TEXTURE_2D, 0, hdr ? gl.RGBA16F : gl.RGBA8, w, h, 0, gl.RGBA, hdr ? gl.HALF_FLOAT : gl.UNSIGNED_BYTE, null);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  const fb = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
  gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
  gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  return {tex, fb, w, h, free() { gl.deleteTexture(tex); gl.deleteFramebuffer(fb); }};
}

// Bloom: the scene goes to a texture, its bright part is blurred at a quarter size, and the two
// are added and tone-mapped onto the canvas. About 0.3 ms on an M-series GPU at 1.5 MP.
function Bloom(gl) {
  const hdr = !!gl.getExtension("EXT_color_buffer_float") || !!gl.getExtension("EXT_color_buffer_half_float");
  const pBright = program(gl, FS_VS, `in vec2 vUv; uniform sampler2D uTex; uniform float uThr; out vec4 o;
    void main(){ vec3 c=texture(uTex,vUv).rgb; float l=max(c.r,max(c.g,c.b)); o=vec4(c*smoothstep(uThr,uThr+0.4,l),1.0); }`);
  const pBlur = program(gl, FS_VS, `in vec2 vUv; uniform sampler2D uTex; uniform vec2 uDir; out vec4 o;
    void main(){ vec3 c=texture(uTex,vUv).rgb*0.227;
      c+=(texture(uTex,vUv+uDir*1.385).rgb+texture(uTex,vUv-uDir*1.385).rgb)*0.316;
      c+=(texture(uTex,vUv+uDir*3.231).rgb+texture(uTex,vUv-uDir*3.231).rgb)*0.070; o=vec4(c,1.0); }`);
  const pComp = program(gl, FS_VS, `in vec2 vUv; uniform sampler2D uTex; uniform sampler2D uBloom; uniform float uStr, uExp; out vec4 o;
    void main(){ vec3 c=texture(uTex,vUv).rgb+texture(uBloom,vUv).rgb*uStr; c=1.0-exp(-c*uExp);
      vec2 e=min(vUv,1.0-vUv); c*=smoothstep(0.0,0.12,e.x)*smoothstep(0.0,0.12,e.y);   // no visible canvas edge
      o=vec4(c,clamp(max(c.r,max(c.g,c.b))*1.15,0.0,1.0)); }`);
  let scene = null, a = null, b = null;
  function size(w, h) {
    if (scene && scene.w === w && scene.h === h) return;
    [scene, a, b].forEach(t => t && t.free());
    scene = target(gl, w, h, hdr);
    const qw = Math.max(1, w >> 2), qh = Math.max(1, h >> 2);
    a = target(gl, qw, qh, hdr); b = target(gl, qw, qh, hdr);
  }
  function pass(prog, dst, vals, tex, tex2) {
    gl.bindFramebuffer(gl.FRAMEBUFFER, dst ? dst.fb : null);
    gl.viewport(0, 0, dst ? dst.w : gl.drawingBufferWidth, dst ? dst.h : gl.drawingBufferHeight);
    gl.useProgram(prog.p);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tex); gl.uniform1i(prog.u.uTex, 0);
    if (tex2) { gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, tex2); gl.uniform1i(prog.u.uBloom, 1); }
    uni(gl, prog, vals); gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
  return {
    hdr,
    begin(w, h) { size(w, h); gl.bindFramebuffer(gl.FRAMEBUFFER, scene.fb); gl.viewport(0, 0, w, h); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT); },
    end(opt) {
      gl.disable(gl.BLEND);
      pass(pBright, a, {uThr: opt.threshold}, scene.tex);
      for (let i = 0; i < 2; i++) {
        pass(pBlur, b, {uDir: [1 / a.w * (1 + i), 0]}, a.tex);
        pass(pBlur, a, {uDir: [0, 1 / a.h * (1 + i)]}, b.tex);
      }
      pass(pComp, null, {uStr: opt.strength, uExp: opt.exposure || 1.6}, scene.tex, a.tex);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, null); gl.activeTexture(gl.TEXTURE0);
    },
    free() { [scene, a, b].forEach(t => t && t.free()); [pBright, pBlur, pComp].forEach(p => gl.deleteProgram(p.p)); },
  };
}

// ------------------------------------------------------------------ the audio level
// A speaking level when nobody supplies one: a syllable-rate envelope, so a voice reply still
// looks like speech. Replaced by the real level as soon as set({level}) or an analyser gives one.
function synthLevel(t) {
  const syll = 0.5 + 0.5 * Math.sin(t * 2 * Math.PI * 4.3 + Math.sin(t * 1.7) * 2.0);
  const phrase = 0.55 + 0.45 * Math.sin(t * 0.9) * Math.sin(t * 0.37 + 1.0);
  return Math.max(0, Math.min(1, syll * phrase * 0.9 + 0.08));
}
// Eight bands out of one level, for orbs that draw a waveform. With an analyser attached they are
// the real spectrum.
function synthBands(level, t, out) {
  for (let i = 0; i < 8; i++) out[i] = level * (0.55 + 0.45 * Math.sin(t * (3.1 + i * 1.7) + i * 2.1)) * (1 - i * 0.07);
  return out;
}

// ------------------------------------------------------------------ mount
// Asked once and the probe context released at once. Browsers keep only about 16 WebGL contexts
// and drop the oldest, so a fresh probe context on every call could cost the live orb its own.
let SUPPORTED = null;
function supported() {
  if (SUPPORTED !== null) return SUPPORTED;
  try {
    const gl = document.createElement("canvas").getContext("webgl2");
    SUPPORTED = !!gl;
    const lose = gl && gl.getExtension("WEBGL_lose_context"); if (lose) lose.loseContext();
  } catch (e) { SUPPORTED = false; }
  return SUPPORTED;
}
// Free a canvas's context now instead of whenever the collector gets to it: for a canvas that
// has left the page, or one being replaced after a context loss.
function release(canvas) {
  try { const gl = canvas.getContext("webgl2"); const lose = gl && !gl.isContextLost() && gl.getExtension("WEBGL_lose_context"); if (lose) lose.loseContext(); } catch (e) { /* nothing to free */ }
}

function mount(canvas, name, opts) {
  opts = Object.assign({bloom: true, motion: true, fps: 0, quality: "balanced", manual: false, stats: false, maxFrames: 0}, opts || {});
  const def = REG[name]; if (!def) throw new Error("no orb called " + name);
  const gl = canvas.getContext("webgl2", {alpha: true, premultipliedAlpha: true, antialias: false, depth: false,
    stencil: false, powerPreference: "low-power", preserveDrawingBuffer: !!opts.preserve});
  if (!gl) return null;
  const reduceMq = root.matchMedia ? root.matchMedia("(prefers-reduced-motion: reduce)") : null;
  const K = {gl, program, uni, vao, M, NOISE, COMMON, FS_VS, rng, opts};
  const scene = def.create(gl, K);
  let bloom = opts.bloom && def.bloom ? Bloom(gl) : null;
  const pal = def.palette;
  const st = {state: "idle", agents: 0, level: null, t_level: 0};
  const sm = {energy: ENERGY.idle, level: 0, agents: 0, phase: 0, vphase: 0, mix: [0, 0, 0, 0], color: pal.idle[0].slice(), color2: pal.idle[1].slice()};
  const agentPh = new Float32Array(12);
  const bands = new Float32Array(8), tgt = [false, false, false, false];
  const U = {uTime: 0, uEnergy: 0, uLevel: 0, uAgents: 0, uMix: sm.mix, uColor: sm.color, uColor2: sm.color2, uRes: [1, 1], uPx: 1, uBands: bands, uPhase: 0, uVPhase: 0, uAgentPh: agentPh};
  const F = {t: 0, w: 1, h: 1, aspect: 1, u: U, st, sm, bands};
  let t = 0, last = 0, running = false, raf = 0, frames = 0, dead = false, visible = true, onScreen = true, analyser = null, abuf = null;
  const ring = {iv: [], cpu: []};
  let timerExt = opts.stats ? gl.getExtension("EXT_disjoint_timer_query_webgl2") : null, queries = [], gpu = [];

  function reduced() { return !opts.motion || (reduceMq && reduceMq.matches); }
  function cap() { return opts.fps || (st.state === "idle" || st.state === "error" ? 30 : 60); }
  let lost = 0;

  function resize() {
    const dpr = Math.min(root.devicePixelRatio || 1, opts.quality === "high" ? 2 : 1.5);
    let w = Math.max(1, Math.round(canvas.clientWidth * dpr * (def.scale || 1))), h = Math.max(1, Math.round(canvas.clientHeight * dpr * (def.scale || 1)));
    const budget = opts.quality === "high" ? 3.2e6 : opts.quality === "low" ? 0.6e6 : 1.6e6;
    if (w * h > budget) { const k = Math.sqrt(budget / (w * h)); w = Math.round(w * k); h = Math.round(h * k); }
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  }

  function advance(dt) {
    t += dt;
    const k = 1 - Math.exp(-dt * 3.2);           // about 0.7 s to settle into a new state
    sm.energy += (ENERGY[st.state] - sm.energy) * k;
    // A run starting or ending eases in over about 1.5 s, one cluster, hub or comet fading at a time.
    sm.agents += (Math.min(12, st.agents || 0) - sm.agents) * (1 - Math.exp(-dt * 2.0));
    tgt[0] = st.state === "listening"; tgt[1] = st.state === "working"; tgt[2] = st.state === "speaking"; tgt[3] = st.state === "error";
    for (let i = 0; i < 4; i++) sm.mix[i] += ((tgt[i] ? 1 : 0) - sm.mix[i]) * k;
    const p = pal[st.state] || pal.idle;
    for (let i = 0; i < 3; i++) { sm.color[i] += (p[0][i] - sm.color[i]) * k; sm.color2[i] += (p[1][i] - sm.color2[i]) * k; }
    let lv = 0;
    if (analyser) {
      analyser.getByteFrequencyData(abuf); let s = 0;
      for (let i = 0; i < 8; i++) { let b = 0; const n = abuf.length >> 3; for (let j = 0; j < n; j++) b += abuf[i * n + j]; bands[i] = b / n / 255; s += bands[i]; }
      lv = Math.min(1, s / 8 * 1.6);
    } else if (st.level != null) lv = st.level;
    else if (st.state === "speaking") lv = synthLevel(t);
    const kl = lv > sm.level ? 1 - Math.exp(-dt * 30) : 1 - Math.exp(-dt * 7);   // fast attack, slower release
    sm.level += (lv - sm.level) * kl;
    if (!analyser) synthBands(sm.level, t, bands);
    sm.phase += dt * sm.energy; sm.vphase += dt * sm.mix[2] * sm.level;
    for (let i = 0; i < 12; i++) agentPh[i] += dt * sm.mix[1] * Math.max(0, Math.min(1, sm.agents - i));
  }

  function render() {
    if (dead) return;
    resize();
    const w = canvas.width, h = canvas.height, c0 = performance.now();
    let q = null;
    if (timerExt && queries.length < 4) { q = gl.createQuery(); gl.beginQuery(timerExt.TIME_ELAPSED_EXT, q); }
    // One frame record and one uniform set, reused every frame: nothing is allocated here.
    const u = U; u.uTime = t; u.uEnergy = sm.energy; u.uLevel = sm.level; u.uAgents = sm.agents; u.uRes[0] = w; u.uRes[1] = h; u.uPx = h / 1000; u.uPhase = sm.phase; u.uVPhase = sm.vphase;
    const f = F; f.t = t; f.w = w; f.h = h; f.aspect = w / h;
    if (bloom) bloom.begin(w, h);
    else { gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.viewport(0, 0, w, h); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT); }
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE);
    scene.draw(f);
    if (bloom) bloom.end(def.bloom);
    if (q) { gl.endQuery(timerExt.TIME_ELAPSED_EXT); queries.push(q); }
    if (timerExt) while (queries.length && gl.getQueryParameter(queries[0], gl.QUERY_RESULT_AVAILABLE)) {
      const q0 = queries.shift(); if (!gl.getParameter(timerExt.GPU_DISJOINT_EXT)) push(gpu, gl.getQueryParameter(q0, gl.QUERY_RESULT) / 1e6); gl.deleteQuery(q0);
    }
    push(ring.cpu, performance.now() - c0);
    frames++;
  }
  function push(a, v) { a.push(v); if (a.length > 240) a.shift(); }

  function tick(now) {
    raf = 0;
    if (!running || dead) return;
    if (!canvas.isConnected) { destroy(); return; }
    // 4 ms of slack: with 1.5 a 30 fps cap on a 120 Hz screen skipped a frame whenever vsync ran
    // early, and the orb alternated 33 and 42 ms frames (judder).
    const min = 1000 / cap() - 4;
    if (last && now - last < min) { raf = requestAnimationFrame(tick); return; }
    const dt = last ? Math.min(0.1, (now - last) / 1000) : 1 / 60;
    if (last) push(ring.iv, now - last);
    last = now;
    advance(dt); render();
    if (opts.maxFrames && frames >= opts.maxFrames) { running = false; return; }
    raf = requestAnimationFrame(tick);
  }
  function wanted() { return !dead && !opts.manual && visible && onScreen && !reduced(); }
  function sync() {
    if (wanted()) { if (!running) { running = true; last = 0; if (!raf) raf = requestAnimationFrame(tick); } }
    else { running = false; if (raf) { cancelAnimationFrame(raf); raf = 0; } }
  }
  // One still frame, settled into the current state: what reduced motion and a paused orb show.
  function still() { if (dead) return; for (let i = 0; i < 40; i++) advance(1 / 30); render(); }

  const onVis = () => { visible = !document.hidden; sync(); };
  document.addEventListener("visibilitychange", onVis);
  let io = null;
  if (root.IntersectionObserver) { io = new IntersectionObserver(es => { onScreen = es[es.length - 1].isIntersecting; sync(); }); io.observe(canvas); }
  const onMq = () => { if (reduced()) still(); sync(); };
  if (reduceMq && reduceMq.addEventListener) reduceMq.addEventListener("change", onMq);
  const onLost = e => { e.preventDefault(); lost++; dead = true; sync(); if (opts.onLost) opts.onLost(); };
  canvas.addEventListener("webglcontextlost", onLost);

  let gone = false;
  function destroy() {
    if (gone) return; gone = true;
    dead = true; running = false; if (raf) cancelAnimationFrame(raf); raf = 0;
    document.removeEventListener("visibilitychange", onVis); if (io) io.disconnect();
    if (reduceMq && reduceMq.removeEventListener) reduceMq.removeEventListener("change", onMq);
    canvas.removeEventListener("webglcontextlost", onLost);
    try { scene.destroy && scene.destroy(); bloom && bloom.free(); } catch (e) { /* context already gone */ }
    if (!canvas.isConnected) release(canvas);   // off the page: nobody will draw on it again
  }
  function pct(a, p) { if (!a.length) return null; const s = a.slice().sort((x, y) => x - y); return +s[Math.min(s.length - 1, Math.floor(s.length * p))].toFixed(3); }

  const h = {
    name, def, gl,
    set(s) {
      if (s.state && STATES.includes(s.state)) st.state = s.state;
      if (s.agents != null) st.agents = Math.max(0, +s.agents || 0);
      if ("level" in s) st.level = s.level == null ? null : Math.max(0, Math.min(1, +s.level));
      if (reduced() && !opts.manual) still();
      sync();
    },
    setOptions(o) { Object.assign(opts, o); if ("bloom" in o) { if (bloom) bloom.free(); bloom = opts.bloom && def.bloom ? Bloom(gl) : null; } if (reduced()) still(); sync(); },
    attachAnalyser(node) { analyser = node; abuf = node ? new Uint8Array(node.frequencyBinCount) : null; },
    step(dt, n) { for (let i = 0; i < (n || 1); i++) advance(dt); render(); },
    pause() { opts.manual = true; sync(); }, resume() { opts.manual = false; sync(); },
    running() { return running; },
    alive() { return !dead; },
    canvas,
    stats() {
      const iv = pct(ring.iv, 0.5);
      return {orb: name, frames, running, cap: cap(), fps: iv ? +(1000 / iv).toFixed(1) : 0, interval_p50: iv, interval_p95: pct(ring.iv, 0.95),
        cpu_p50: pct(ring.cpu, 0.5), cpu_p95: pct(ring.cpu, 0.95), gpu_p50: pct(gpu, 0.5), gpu_p95: pct(gpu, 0.95),
        size: [canvas.width, canvas.height], bloom: !!bloom, hdr: bloom ? bloom.hdr : null, renderer: rendererName(gl), lost, t: +t.toFixed(2)};
    },
    resetStats() { ring.iv.length = 0; ring.cpu.length = 0; gpu.length = 0; last = 0; },
    readPixels() {   // mean colour and the lit fraction of what is on the canvas now
      const w = gl.drawingBufferWidth, hh = gl.drawingBufferHeight, px = new Uint8Array(w * hh * 4);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.readPixels(0, 0, w, hh, gl.RGBA, gl.UNSIGNED_BYTE, px);
      let r = 0, g = 0, b = 0, lit = 0, hash = 0; const n = w * hh;
      for (let i = 0; i < n; i++) { const o = i * 4; r += px[o]; g += px[o + 1]; b += px[o + 2]; if (px[o] + px[o + 1] + px[o + 2] > 60) lit++; if ((i & 63) === 0) hash = (hash * 31 + px[o] + px[o + 1] * 3 + px[o + 2] * 7) >>> 0; }
      return {r: r / n, g: g / n, b: b / n, lit: lit / n, hash, px, w, h: hh};
    },
    destroy,
  };
  resize();
  if (reduced() && !opts.manual) still(); else render();
  sync();
  return h;
}

function rendererName(gl) {
  try { const e = gl.getExtension("WEBGL_debug_renderer_info"); return e ? gl.getParameter(e.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER); } catch (e) { return "?"; }
}

// ------------------------------------------------------------------ self-test
// Every orb, every state, rendered small and read back. Proves each orb compiles, draws something
// in every state, tells the states apart, reacts to the agent count and to the audio level, and
// that the core stops drawing when the page is hidden.
function selftest(name, size) {
  const checks = [], ok = (n, c, d) => checks.push([n, !!c, d == null ? "" : String(d)]);
  const def = REG[name];
  ok("registered", def, name); if (!def) return {orb: name, ok: false, checks};
  ok("has a label", typeof def.label === "string" && def.label.length > 3, def.label);
  ok("palette covers every state", STATES.every(s => Array.isArray(def.palette[s]) && def.palette[s].length === 2), "");
  if (def.selftest) for (const c of def.selftest(rng)) checks.push([c[0], !!c[1], c[2] == null ? "" : String(c[2])]);
  const cv = document.createElement("canvas"); cv.style.cssText = `width:${size || 200}px;height:${Math.round((size || 200) / 1.3)}px;position:absolute;left:-9999px`;
  document.body.appendChild(cv);
  let h;
  try { h = mount(cv, name, {manual: true, bloom: true, quality: "low"}); } catch (e) { ok("mounts", false, e.message); cv.remove(); return {orb: name, ok: false, checks}; }
  ok("mounts with WebGL2", h, h ? rendererName(h.gl) : "no webgl2");
  if (!h) { cv.remove(); return {orb: name, ok: false, checks}; }
  const shot = s => { h.set(s); h.step(1 / 30, 45); return h.readPixels(); };
  const got = {};
  for (const s of STATES) {
    const p = shot({state: s, agents: s === "working" ? 3 : 0, level: s === "speaking" ? 0.7 : null});
    got[s] = p;
    ok(`${s}: draws something`, p.lit > 0.002, `lit ${(p.lit * 100).toFixed(2)}%`);
    ok(`${s}: not a white-out`, p.lit < 0.9, `lit ${(p.lit * 100).toFixed(2)}%`);
    ok(`${s}: no GL error`, h.gl.getError() === 0, "");
  }
  const col = p => [p.r, p.g, p.b], dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]) / (Math.hypot(...a) + Math.hypot(...b) + 1e-6);
  let distinct = 0;
  for (let i = 0; i < STATES.length; i++) for (let j = i + 1; j < STATES.length; j++) if (dist(col(got[STATES[i]]), col(got[STATES[j]])) > 0.02 || got[STATES[i]].hash !== got[STATES[j]].hash) distinct++;
  ok("the five states look different", distinct === 10, `${distinct} of 10 pairs differ`);
  const redness = p => p.r / (p.g + p.b + 1e-6);
  ok("error is redder than idle", redness(got.error) > redness(got.idle) * 1.2, `${redness(got.error).toFixed(2)} vs ${redness(got.idle).toFixed(2)}`);
  const a0 = shot({state: "working", agents: 0}), a6 = shot({state: "working", agents: 6});
  ok("the agent count changes the picture", a0.hash !== a6.hash && Math.abs(a6.lit - a0.lit) + dist(col(a6), col(a0)) > 0.0005,
    `lit ${(a0.lit * 100).toFixed(2)}% -> ${(a6.lit * 100).toFixed(2)}%`);
  const l1 = shot({state: "speaking", level: 0.05}), l9 = shot({state: "speaking", level: 1});
  ok("the audio level changes the picture", l1.hash !== l9.hash, `lit ${(l1.lit * 100).toFixed(2)}% -> ${(l9.lit * 100).toFixed(2)}%`);
  h.destroy(); cv.remove();
  return {orb: name, ok: checks.every(c => c[1]), checks};
}

// The pause rules, on a live loop: frames stop when the page reports hidden, and restart after.
function pausetest(name) {
  return new Promise(resolve => {
    const cv = document.createElement("canvas"); cv.style.cssText = "width:160px;height:120px;position:fixed;left:0;top:0;opacity:0.01";
    document.body.appendChild(cv);
    let h;
    try { h = mount(cv, name, {bloom: false, fps: 60, quality: "low"}); } catch (e) { h = null; }
    if (!h) { cv.remove(); resolve({ok: false, checks: [["mounts for the pause test", false, name]]}); return; }
    h.set({state: "working", agents: 2});
    const desc = Object.getOwnPropertyDescriptor(Document.prototype, "hidden");
    const hide = v => { Object.defineProperty(document, "hidden", {configurable: true, get: () => v}); document.dispatchEvent(new Event("visibilitychange")); };
    setTimeout(() => {
      const f1 = h.stats().frames; hide(true);
      setTimeout(() => {
        const f2 = h.stats().frames;
        setTimeout(() => {
          const f3 = h.stats().frames; hide(false); delete document.hidden; if (desc) { /* the prototype getter is back */ }
          setTimeout(() => {
            const f4 = h.stats().frames; h.destroy(); cv.remove();
            resolve({ok: f1 > 3 && f3 - f2 === 0 && f4 > f3,
              checks: [["runs while visible", f1 > 3, `${f1} frames`], ["no frames while hidden", f3 - f2 === 0, `${f3 - f2} frames in 400 ms hidden`],
                ["resumes when shown", f4 > f3, `${f4 - f3} frames after`]]});
          }, 300);
        }, 400);
      }, 50);
    }, 300);
  });
}

root.PolarisOrbs = {STATES, register, mount, selftest, pausetest, supported, release, list: () => Object.keys(REG).map(k => ({name: k, label: REG[k].label, mode: REG[k].mode || "", blurb: REG[k].blurb || ""})),
  _internal: {M, rng, synthLevel, synthBands, REG}};
})(typeof window !== "undefined" ? window : globalThis);
