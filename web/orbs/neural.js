/* neural.js - the neural sphere: a golden sphere of connected nodes over a perspective floor grid (the
   default orb on the Polaris Home screen).

   420 nodes on and inside a sphere, spread by a few rounds of force-directed repulsion at load,
   each wired to its nearest neighbours (about 900 edges). Signals run along the edges as pulses
   computed in the fragment shader, so the CPU does nothing per frame beyond one matrix. The
   sphere breathes with simplex noise; the grid below scrolls toward the viewer while work runs.

   States: idle is a slow amber turn with rare signals. Listening sweeps a bright wave through the
   sphere front to back. Working lights one hub per running agent (up to twelve) and floods the
   edges around it with signals. Speaking swells the sphere with the audio level. Error goes red
   and drops edges out.
*/
(function () {
"use strict";
const SURF = 360, INNER = 60, NODES = SURF + INNER, HUBS = 12, K_NEAR = 3;

function geometry(rng) {
  const r = rng(4201), pos = [];
  const ga = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < SURF; i++) {                     // a Fibonacci sphere, then jittered
    const y = 1 - (i + 0.5) / SURF * 2, rad = Math.sqrt(1 - y * y), th = ga * i;
    pos.push([Math.cos(th) * rad + (r() - 0.5) * 0.12, y + (r() - 0.5) * 0.12, Math.sin(th) * rad + (r() - 0.5) * 0.12]);
  }
  for (let i = 0; i < INNER; i++) {
    const u = r() * 2 - 1, th = r() * 6.283, s = Math.sqrt(1 - u * u), rr = 0.35 + r() * 0.45;
    pos.push([s * Math.cos(th) * rr, u * rr, s * Math.sin(th) * rr]);
  }
  // Force-directed relaxation: neighbours push apart, the surface nodes are pulled back onto the
  // sphere. Twelve rounds is enough to lose the Fibonacci pattern and keep the spacing even.
  for (let it = 0; it < 12; it++) {
    const f = pos.map(() => [0, 0, 0]);
    for (let i = 0; i < NODES; i++) for (let j = i + 1; j < NODES; j++) {
      const dx = pos[i][0] - pos[j][0], dy = pos[i][1] - pos[j][1], dz = pos[i][2] - pos[j][2], d2 = dx * dx + dy * dy + dz * dz + 1e-4;
      if (d2 > 0.09) continue;
      const k = 0.0009 / d2;
      f[i][0] += dx * k; f[i][1] += dy * k; f[i][2] += dz * k; f[j][0] -= dx * k; f[j][1] -= dy * k; f[j][2] -= dz * k;
    }
    for (let i = 0; i < NODES; i++) {
      const p = pos[i]; p[0] += f[i][0]; p[1] += f[i][1]; p[2] += f[i][2];
      if (i < SURF) { const l = Math.hypot(p[0], p[1], p[2]); const t = 1 + (r() - 0.5) * 0.06; p[0] *= t / l; p[1] *= t / l; p[2] *= t / l; }
    }
  }
  // Hubs: twelve surface nodes spread around the sphere, one per possible running agent.
  const hubs = [];
  for (let h = 0; h < HUBS; h++) hubs.push(Math.floor((h + 0.5) / HUBS * SURF));
  const hubOf = new Float32Array(NODES).fill(99);
  hubs.forEach((n, h) => { hubOf[n] = h; });
  // Edges: each node to its K nearest; the hub id spreads two hops so a lit hub lights a patch.
  const edges = new Set(), near = [];
  for (let i = 0; i < NODES; i++) {
    const ds = [];
    for (let j = 0; j < NODES; j++) if (j !== i) ds.push([(pos[i][0] - pos[j][0]) ** 2 + (pos[i][1] - pos[j][1]) ** 2 + (pos[i][2] - pos[j][2]) ** 2, j]);
    ds.sort((a, b) => a[0] - b[0]);
    near.push(ds.slice(0, K_NEAR + 2).map(d => d[1]));
    for (const [, j] of ds.slice(0, K_NEAR)) edges.add(i < j ? i + ":" + j : j + ":" + i);
  }
  const zone = Float32Array.from(hubOf);
  for (let hop = 0; hop < 2; hop++) {
    const nz = Float32Array.from(zone);
    for (let i = 0; i < NODES; i++) if (zone[i] < 99) for (const j of near[i]) if (nz[j] >= 99) nz[j] = zone[i];
    zone.set(nz);
  }
  const E = [...edges].map(s => s.split(":").map(Number));
  const nodeA = new Float32Array(NODES * 4), nodeB = new Float32Array(NODES * 4);
  for (let i = 0; i < NODES; i++) { nodeA.set([...pos[i], 1 + r() * 1.6 + (hubOf[i] < 99 ? 1.5 : 0)], i * 4); nodeB.set([r(), hubOf[i], zone[i], i < SURF ? 1 : 0.6], i * 4); }
  const edgeP = new Float32Array(E.length * 2 * 3), edgeB = new Float32Array(E.length * 2 * 4);
  E.forEach(([a, b], k) => {
    const seed = r(), z = Math.min(zone[a], zone[b]);
    edgeP.set(pos[a], k * 6); edgeP.set(pos[b], k * 6 + 3);
    edgeB.set([0, seed, z, r()], k * 8); edgeB.set([1, seed, z, r()], k * 8 + 4);
  });
  return {nodeA, nodeB, edgeP, edgeB, nEdges: E.length, pos};
}

// The shared displacement, so edges stay attached to their nodes while the sphere breathes.
const WARP = `
vec3 warp(vec3 p){
  float s=1.0+0.045*snoise(p*1.6+vec3(0.0,uTime*0.18,0.0))*(0.6+uEnergy);
  s*=1.0+uMix.z*uLevel*0.14;
  return p*s;
}`;
const NODE_VS = `
layout(location=0) in vec4 aP; layout(location=1) in vec4 aB;
uniform mat4 uPV, uModel;
__COMMON__
__NOISE__
__WARP__
out vec3 vCol; out float vA;
void main(){
  vec4 wp=uModel*vec4(warp(aP.xyz),1.0);
  gl_Position=uPV*wp;
  float depth=smoothstep(-1.2,1.2,wp.z);
  float hubOn=clamp(uAgents-aB.y,0.0,1.0)*uMix.y;
  float wave=uMix.x*exp(-pow(wp.z-(fract(uTime*0.45)*3.0-1.5),2.0)*14.0);
  float spark=0.65+0.35*sin(uTime*(1.0+aB.x*2.0)+aB.x*30.0);
  float sz=aP.w*(0.8+0.5*depth)*(1.0+hubOn*1.6+wave*0.8+uMix.z*uLevel*0.6);
  gl_PointSize=max(1.5,sz*uPx*9.0/gl_Position.w);
  vCol=mix(uColor,vec3(1.0,0.95,0.85),0.25+hubOn*0.6+wave*0.5);
  vA=(0.45+0.9*depth)*spark*aB.w*(0.55+0.6*uEnergy)*(1.0+hubOn*2.5+wave*1.6+uMix.z*uLevel);
}`;
const NODE_FS = `in vec3 vCol; in float vA; out vec4 o;
void main(){ vec2 d=gl_PointCoord-0.5; float r2=dot(d,d); float f=(exp(-r2*22.0)+0.35*exp(-r2*6.0))*(1.0-smoothstep(0.16,0.25,r2)); o=vec4(vCol*vA*f,0.0); }`;
const EDGE_VS = `
layout(location=0) in vec3 aP; layout(location=1) in vec4 aB;
uniform mat4 uPV, uModel;
__COMMON__
__NOISE__
__WARP__
out float vT; out float vSeed; out float vZone; out float vDepth; out float vR;
void main(){
  vec4 wp=uModel*vec4(warp(aP),1.0);
  gl_Position=uPV*wp;
  vT=aB.x; vSeed=aB.y; vZone=aB.z; vR=aB.w; vDepth=smoothstep(-1.2,1.2,wp.z);
}`;
const EDGE_FS = `
__COMMON__
in float vT; in float vSeed; in float vZone; in float vDepth; in float vR; out vec4 o;
void main(){
  float zoneOn=clamp(uAgents-vZone,0.0,1.0)*uMix.y;
  float zph=vZone<11.5?uAgentPh[int(vZone)]:0.0;
  float busy=0.06+0.1*uEnergy+zoneOn*0.75+uMix.x*0.25;
  float act=step(fract(vSeed*7.31),busy);
  float rate=0.25+0.9*uEnergy+zoneOn*0.8;
  float head=fract((0.25*uTime+0.9*uPhase+0.8*zph)*(0.6+vR)+vSeed*11.0);   // rate, integrated: see uPhase in orb_core.js
  float dir=step(0.5,vR); float tt=mix(vT,1.0-vT,dir);
  float pulse=act*exp(-pow((tt-head)*9.0,2.0));
  float base=(0.09+0.2*vDepth)*(0.6+0.5*uEnergy);
  float drop=1.0-uMix.w*step(0.55,fract(vSeed*23.0+floor(uTime*5.0)*0.31));
  vec3 c=uColor*base+mix(uColor,vec3(1.0,0.97,0.9),0.5)*pulse*(1.2+zoneOn);
  o=vec4(c*drop,0.0);
}`;
// The floor: a grid on the plane y = FLOOR, lines drawn by the fragment shader from world x,z, so
// it costs one quad. A pool of light sits under the sphere.
const GRID_VS = `
layout(location=0) in vec2 aXZ;
uniform mat4 uPV; uniform float uFloor;
out vec2 vXZ;
void main(){ vXZ=aXZ; gl_Position=uPV*vec4(aXZ.x,uFloor,aXZ.y,1.0); }`;
const GRID_FS = `
__COMMON__
in vec2 vXZ; out vec4 o;
void main(){
  float sc=0.42; vec2 p=vXZ/sc; p.y+=uTime*uMix.y*0.6;
  vec2 g=abs(fract(p-0.5)-0.5)/fwidth(p);
  float line=1.0-min(min(g.x,g.y),1.0);
  float d=length(vXZ);
  float fade=(1.0-smoothstep(0.8,4.0,d))*(0.13+0.1*uEnergy);
  float pool=exp(-d*d*1.4)*(0.18+0.25*uEnergy+uMix.z*uLevel*0.4);
  o=vec4(uColor*(line*fade+pool*0.35),0.0);
}`;

PolarisOrbs.register("neural", {
  label: "neural sphere",
  mode: "default",
  blurb: "420 nodes, ~900 edges with travelling signals, over a floor grid. One hub lights per running agent.",
  palette: {
    idle: [[1.0, 0.7, 0.3], [1.0, 0.85, 0.55]],
    listening: [[1.0, 0.86, 0.5], [1.0, 0.95, 0.75]],
    working: [[1.0, 0.62, 0.2], [1.0, 0.8, 0.45]],
    speaking: [[1.0, 0.78, 0.42], [1.0, 0.9, 0.7]],
    error: [[1.0, 0.28, 0.25], [1.0, 0.5, 0.4]],
  },
  bloom: {strength: 1.0, threshold: 0.28, exposure: 1.6},
  geometry,
  create(gl, K) {
    const g = geometry(K.rng), sub = s => s.replace("__COMMON__", K.COMMON).replace("__NOISE__", K.NOISE).replace("__WARP__", WARP);
    const pn = K.program(gl, sub(NODE_VS), NODE_FS), pe = K.program(gl, sub(EDGE_VS), sub(EDGE_FS)), pg = K.program(gl, GRID_VS, sub(GRID_FS));
    const vn = K.vao(gl, [{data: g.nodeA, size: 4, loc: 0}, {data: g.nodeB, size: 4, loc: 1}]);
    const ve = K.vao(gl, [{data: g.edgeP, size: 3, loc: 0}, {data: g.edgeB, size: 4, loc: 1}]);
    const S = 7, vg = K.vao(gl, [{data: new Float32Array([-S, -S, S, -S, S, S, -S, -S, S, S, -S, S]), size: 2, loc: 0}]);
    return {
      draw(f) {
        const P = K.M.perspective(0.62, f.aspect, 0.1, 40), V = K.M.lookAt([0, 0.55, 4.3], [0, -0.28, 0], [0, 1, 0]), PV = K.M.mul(P, V);
        const spin = 0.06 * f.t + 0.16 * f.sm.phase;   // not f.t * speed(energy): see uPhase in orb_core.js
        const model = K.M.mul(K.M.rotX(0.28), K.M.rotY(spin));
        const u = Object.assign({uPV: PV, uModel: model, uFloor: -1.45}, f.u);
        gl.useProgram(pg.p); K.uni(gl, pg, u); gl.bindVertexArray(vg.v); gl.drawArrays(gl.TRIANGLES, 0, 6);
        gl.useProgram(pe.p); K.uni(gl, pe, u); gl.bindVertexArray(ve.v); gl.drawArrays(gl.LINES, 0, g.nEdges * 2);
        gl.useProgram(pn.p); K.uni(gl, pn, u); gl.bindVertexArray(vn.v); gl.drawArrays(gl.POINTS, 0, NODES);
        gl.bindVertexArray(null);
      },
      destroy() { [vn, ve, vg].forEach(v => v.free()); [pn, pe, pg].forEach(p => gl.deleteProgram(p.p)); },
    };
  },
  selftest(rng) {
    const g = geometry(rng);
    const finite = g.nodeA.every(Number.isFinite) && g.edgeP.every(Number.isFinite);
    let onSphere = 0; for (let i = 0; i < SURF; i++) { const l = Math.hypot(...g.pos[i]); if (l > 0.9 && l < 1.1) onSphere++; }
    const hubs = new Set(); for (let i = 0; i < NODES; i++) if (g.nodeB[i * 4 + 1] < 99) hubs.add(g.nodeB[i * 4 + 1]);
    let minD = 9; for (let i = 0; i < SURF; i += 7) for (let j = 0; j < SURF; j++) if (i !== j) minD = Math.min(minD, Math.hypot(g.pos[i][0] - g.pos[j][0], g.pos[i][1] - g.pos[j][1], g.pos[i][2] - g.pos[j][2]));
    return [["neural geometry is finite", finite], ["neural surface nodes sit on the sphere", onSphere === SURF, `${onSphere}/${SURF}`],
      ["neural has 600 to 1400 edges", g.nEdges > 600 && g.nEdges < 1400, g.nEdges], ["neural has twelve hubs", hubs.size === HUBS, hubs.size],
      ["relaxation keeps nodes apart", minD > 0.02, minD.toFixed(3)]];
  },
});
})();
