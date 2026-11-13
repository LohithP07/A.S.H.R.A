/* A.S.H.R.A — Knowledge Galaxy
   A single-page 3d-force-graph viewer. No build step. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const loader = $('loader');
  const loaderSub = $('loader-sub');
  const stage = $('stage');
  const panel = $('panel');
  const panelScroll = $('panel-scroll');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const GROUP_COLORS = {
    Strategy: '#a78bfa',
    Operations: '#22d3ee',
    Marketing: '#f472b6',
    Finance: '#fbbf24',
    People: '#34d399',
    Products: '#fb7185',
    Customers: '#60a5fa',
    Captures: '#a3e635'
  };
  const FALLBACK_COLORS = ['#c4b5fd', '#67e8f9', '#f9a8d4', '#fde68a', '#6ee7b7', '#fda4af', '#93c5fd'];

  function fail(message) {
    loader.classList.add('error');
    loader.classList.remove('done');
    loaderSub.textContent = message;
    const title = loader.querySelector('.loader-title');
    if (title) title.textContent = 'Signal lost';
  }

  if (!window.GRAPH || !Array.isArray(window.GRAPH.nodes) || !window.GRAPH.nodes.length) {
    fail('No graph data found. Run "python build.py" from the project folder, then reload this page.');
    return;
  }
  if (typeof THREE === 'undefined' || typeof ForceGraph3D === 'undefined') {
    fail('The 3D library could not load. Check your internet connection, then reload.');
    return;
  }

  const DATA = window.GRAPH;
  const nodes = DATA.nodes.map((node) => ({ ...node }));
  const links = DATA.links.map((link) => ({ ...link }));

  // Numeric ids are indexes by design. Keep this lookup available for deep links.
  const nodeByIndex = nodes;

  const colorFor = (group) => GROUP_COLORS[group] || '#94a3b8';
  const colorCache = new Map();
  const palette = new Map();

  // ---------- adjacency ----------

  const adjacency = new Map();
  const relation = new Map();
  const RELATION_RANK = { 'mentions title': 1, wikilink: 2 };

  function rebuildRelations() {
    adjacency.clear();
    relation.clear();
    nodes.forEach((node) => adjacency.set(node.id, new Set()));

    links.forEach((link) => {
      const a = Number(link.source);
      const b = Number(link.target);
      if (!adjacency.has(a) || !adjacency.has(b)) return;
      adjacency.get(a).add(b);
      adjacency.get(b).add(a);
      const key = a < b ? `${a}-${b}` : `${b}-${a}`;
      const previous = relation.get(key);
      if (!previous || (RELATION_RANK[link.kind] || 0) > (RELATION_RANK[previous] || 0)) {
        relation.set(key, link.kind);
      }
    });

    nodes.forEach((node, index) => {
      node.degree = adjacency.get(node.id).size;
      node.__phase = (index * 2.399963) % (Math.PI * 2);
    });
  }

  rebuildRelations();

  function pairKind(a, b) {
    return relation.get(a < b ? `${a}-${b}` : `${b}-${a}`) || 'mentions title';
  }

  function endpointId(endpoint) {
    return endpoint && typeof endpoint === 'object' ? endpoint.id : endpoint;
  }

  // ---------- dynamic labels ----------

  const legendList = $('legend-list');

  function updateStats() {
    $('stat-notes').textContent = String(nodes.length);
    $('stat-links').textContent = String(links.length);
  }

  function rebuildLegend() {
    const counts = {};
    const list = [...new Set(nodes.map((node) => node.group))];
    nodes.forEach((node) => { counts[node.group] = (counts[node.group] || 0) + 1; });

    list.forEach((group, index) => {
      if (!GROUP_COLORS[group]) GROUP_COLORS[group] = FALLBACK_COLORS[index % FALLBACK_COLORS.length];
      palette.set(group, colorFor(group));
    });

    legendList.replaceChildren();
    list
      .sort((a, b) => (counts[b] - counts[a]) || a.localeCompare(b))
      .forEach((group) => {
        const row = document.createElement('div');
        row.className = 'legend-item';
        const dot = document.createElement('span');
        dot.className = 'legend-dot';
        dot.style.background = colorFor(group);
        dot.style.color = colorFor(group);
        const name = document.createElement('span');
        name.textContent = group;
        const count = document.createElement('span');
        count.className = 'legend-count';
        count.textContent = String(counts[group]);
        row.append(dot, name, count);
        legendList.append(row);
      });

    return list;
  }

  const groups = rebuildLegend();
  updateStats();
  loaderSub.textContent = `Mapping ${nodes.length} notes across ${groups.length} constellations`;

  // ---------- textures ----------

  function makeTexture(size, paint) {
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d');
    paint(ctx, size);
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.needsUpdate = true;
    return texture;
  }

  const glowTexture = makeTexture(256, (ctx, size) => {
    const half = size / 2;
    const gradient = ctx.createRadialGradient(half, half, 0, half, half, half);
    gradient.addColorStop(0, 'rgba(255,255,255,1)');
    gradient.addColorStop(0.12, 'rgba(255,255,255,0.96)');
    gradient.addColorStop(0.28, 'rgba(255,255,255,0.46)');
    gradient.addColorStop(0.52, 'rgba(255,255,255,0.14)');
    gradient.addColorStop(0.78, 'rgba(255,255,255,0.03)');
    gradient.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, size, size);
  });

  const coreTexture = makeTexture(128, (ctx, size) => {
    const half = size / 2;
    const gradient = ctx.createRadialGradient(half, half, 0, half, half, half);
    gradient.addColorStop(0, 'rgba(255,255,255,1)');
    gradient.addColorStop(0.28, 'rgba(255,255,255,1)');
    gradient.addColorStop(0.55, 'rgba(255,255,255,0.52)');
    gradient.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, size, size);
  });

  const ringTexture = makeTexture(256, (ctx, size) => {
    const half = size / 2;
    ctx.translate(half, half);
    ctx.strokeStyle = 'rgba(255,255,255,0.9)';
    ctx.lineWidth = 5;
    ctx.shadowColor = 'rgba(255,255,255,0.9)';
    ctx.shadowBlur = 14;
    ctx.beginPath();
    ctx.arc(0, 0, 92, 0, Math.PI * 2);
    ctx.stroke();
    ctx.globalAlpha = 0.38;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(0, 0, 108, 0, Math.PI * 2);
    ctx.stroke();
  });

  const starTexture = makeTexture(64, (ctx, size) => {
    const half = size / 2;
    const gradient = ctx.createRadialGradient(half, half, 0, half, half, half);
    gradient.addColorStop(0, 'rgba(255,255,255,1)');
    gradient.addColorStop(0.24, 'rgba(255,255,255,0.9)');
    gradient.addColorStop(0.55, 'rgba(255,255,255,0.25)');
    gradient.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, size, size);
  });

  const nebulaTexture = makeTexture(512, (ctx, size) => {
    ctx.clearRect(0, 0, size, size);
    ctx.globalCompositeOperation = 'lighter';
    for (let i = 0; i < 34; i += 1) {
      const x = size * (0.22 + Math.random() * 0.56);
      const y = size * (0.22 + Math.random() * 0.56);
      const radius = size * (0.08 + Math.random() * 0.28);
      const gradient = ctx.createRadialGradient(x, y, 0, x, y, radius);
      gradient.addColorStop(0, `rgba(255,255,255,${0.1 + Math.random() * 0.2})`);
      gradient.addColorStop(0.45, `rgba(255,255,255,${0.04 + Math.random() * 0.08})`);
      gradient.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = gradient;
      ctx.fillRect(0, 0, size, size);
    }
  });

  function mixColor(a, b, amount) {
    const start = new THREE.Color(a);
    const end = new THREE.Color(b);
    return `#${start.lerp(end, amount).getHexString()}`;
  }

  function makeLabelSprite(text, color) {
    const fontFamily = 'Inter, ui-sans-serif, system-ui, sans-serif';
    let fontSize = 40;
    const measuring = document.createElement('canvas').getContext('2d');
    measuring.font = `500 ${fontSize}px ${fontFamily}`;
    let textWidth = measuring.measureText(text).width;
    while (textWidth > 520 && fontSize > 28) {
      fontSize -= 2;
      measuring.font = `500 ${fontSize}px ${fontFamily}`;
      textWidth = measuring.measureText(text).width;
    }

    const padding = 26;
    const canvas = document.createElement('canvas');
    canvas.width = Math.ceil(textWidth + padding * 2);
    canvas.height = 72;
    const ctx = canvas.getContext('2d');
    ctx.font = `500 ${fontSize}px ${fontFamily}`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.shadowColor = 'rgba(0, 0, 0, 0.95)';
    ctx.shadowBlur = 12;
    ctx.fillStyle = mixColor(color, '#ffffff', 0.7);
    ctx.fillText(text, canvas.width / 2, canvas.height / 2 + 1);

    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.needsUpdate = true;
    const material = new THREE.SpriteMaterial({
      map: texture,
      transparent: true,
      opacity: 0.7,
      depthWrite: false,
      depthTest: false
    });
    const sprite = new THREE.Sprite(material);
    const worldPerPixel = 0.155;
    sprite.scale.set(canvas.width * worldPerPixel, canvas.height * worldPerPixel, 1);
    sprite.center.set(0.5, 0.5);
    sprite.renderOrder = 4;
    return sprite;
  }

  function makeNodeObject(node) {
    const color = colorFor(node.group);
    const size = 27 + Math.min(node.degree, 15) * 2.3;
    const coreSize = size * 0.16 + 3.6;

    const halo = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTexture,
      color: new THREE.Color(color),
      transparent: true,
      opacity: 0.82,
      depthWrite: false,
      depthTest: true,
      blending: THREE.AdditiveBlending
    }));
    halo.scale.set(size, size, 1);
    halo.renderOrder = 1;

    const ring = new THREE.Sprite(new THREE.SpriteMaterial({
      map: ringTexture,
      color: new THREE.Color(color),
      transparent: true,
      opacity: 0,
      depthWrite: false,
      depthTest: false,
      blending: THREE.AdditiveBlending
    }));
    ring.scale.set(size * 1.86, size * 1.86, 1);
    ring.renderOrder = 2;

    const core = new THREE.Sprite(new THREE.SpriteMaterial({
      map: coreTexture,
      color: new THREE.Color(mixColor(color, '#ffffff', 0.6)),
      transparent: true,
      opacity: 1,
      depthWrite: false,
      depthTest: true,
      blending: THREE.AdditiveBlending
    }));
    core.scale.set(coreSize, coreSize, 1);
    core.renderOrder = 3;

    const label = makeLabelSprite(node.label, color);
    const above = node.id % 2 === 1;
    label.position.set(0, above ? size * 0.55 + 10 : -(size * 0.52 + 11), 0);

    const group = new THREE.Group();
    group.add(halo, ring, core, label);
    node.__viz = { halo, ring, core, label, size, coreSize, active: true, selected: false };
    return group;
  }

  // ---------- force graph ----------

  // Seed the layout on a flattened Fibonacci sphere so the initial camera
  // reveal has depth before the force simulation settles.
  nodes.forEach((node, index) => {
    const n = nodes.length;
    const y = 1 - (index / Math.max(n - 1, 1)) * 2;
    const ring = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = Math.PI * (1 + Math.sqrt(5)) * index;
    const radius = 160 + (index % 7) * 19;
    node.x = Math.cos(theta) * ring * radius;
    node.y = y * radius * 0.72;
    node.z = Math.sin(theta) * ring * radius;
  });

  links.forEach((link) => {
    const a = Number(link.source);
    const b = Number(link.target);
    link.source = a;
    link.target = b;
    link.__active = true;
    link.__particles = 0;
  });

  let focusedNode = null;
  let hoveredNode = null;
  let driftTimer = 0;
  let driftWanted = false;
  let cameraFlight = null;
  let labelsVisible = true;
  let controls = null;
  let graph = null;
  let clusterIds = null;
  let clusterTint = null;
  const starLayers = [];

  function linkColor(link) {
    if (!link.__active) return '#08101a';
    if (focusedNode) return colorFor(focusedNode.group);
    if (clusterTint) return clusterTint;
    return '#2a3a51';
  }

  function linkWidth(link) {
    if (!link.__active) return 0.12;
    return focusedNode ? 0.9 : 0.4;
  }

  function initGraph() {
    graph = ForceGraph3D()(stage)
      .graphData({ nodes, links })
      .nodeId('id')
      .nodeLabel((node) => `${node.label}   ·   ${node.group}`)
      .nodeThreeObject(makeNodeObject)
      .nodeThreeObjectExtend(false)
      .linkColor(linkColor)
      .linkWidth(linkWidth)
      .linkOpacity(0.58)
      .linkResolution(4)
      .linkDirectionalParticles((link) => link.__particles)
      .linkDirectionalParticleWidth(1.7)
      .linkDirectionalParticleSpeed(0.006)
      .linkDirectionalParticleColor(() => (focusedNode ? colorFor(focusedNode.group) : '#d6e1f2'))
      .backgroundColor('#010206')
      .showNavInfo(false)
      .enableNodeDrag(true)
      .onNodeClick((node) => { if (node) focusNode(node); })
      .onNodeHover((node) => {
        hoveredNode = node || null;
        const canvas = stage.querySelector('canvas');
        if (canvas) canvas.style.cursor = node ? 'pointer' : 'grab';
      })
      .onBackgroundClick(() => {
        if (focusedNode) dismissFocus();
        else if (clusterIds) clearFocus();
      })
      .cooldownTicks(150)
      .cooldownTime(6000)
      .warmupTicks(220)
      .d3AlphaDecay(0.02)
      .d3VelocityDecay(0.26);

    const charge = graph.d3Force('charge');
    if (charge) charge.strength(-420).distanceMax(1000);
    const linkForce = graph.d3Force('link');
    if (linkForce) linkForce.distance(100).strength(0.25);

    controls = graph.controls();
    controls.enableDamping = true;
    controls.dampingFactor = 0.06;
    controls.rotateSpeed = 0.55;
    controls.zoomSpeed = 0.8;
    controls.autoRotateSpeed = 0.32;
    controls.minDistance = 70;
    controls.maxDistance = 2400;

    controls.addEventListener('start', () => {
      stopDrift();
      clearTimeout(driftTimer);
    });
    controls.addEventListener('end', () => {
      if (!focusedNode && !panel.classList.contains('open')) scheduleDrift(8000);
    });

    buildSpace(graph.scene());

    graph.cameraPosition({ x: 196, y: 122, z: 612 }, { x: 0, y: 0, z: 0 }, 0);
    controls.target.set(0, 0, 0);
    controls.update();
    window.addEventListener('resize', () => {
      graph.width(window.innerWidth).height(window.innerHeight);
    });
  }

  function buildSpace(scene) {
    scene.background = new THREE.Color('#010206');

    const layers = [
      { count: 2200, min: 950, max: 2700, size: 4.6, opacity: 1 },
      { count: 550, min: 600, max: 1500, size: 7.0, opacity: 0.9 },
      { count: 90, min: 380, max: 900, size: 10.0, opacity: 0.65 }
    ];

    layers.forEach((spec, layerIndex) => {
      const positions = new Float32Array(spec.count * 3);
      const colors = new Float32Array(spec.count * 3);
      const color = new THREE.Color();

      for (let i = 0; i < spec.count; i += 1) {
        const u = Math.random() * 2 - 1;
        const theta = Math.random() * Math.PI * 2;
        const radius = spec.min + Math.random() * (spec.max - spec.min);
        const planar = Math.sqrt(Math.max(0, 1 - u * u));

        positions[i * 3] = radius * planar * Math.cos(theta);
        positions[i * 3 + 1] = radius * u;
        positions[i * 3 + 2] = radius * planar * Math.sin(theta);

        if (Math.random() < 0.12) {
          color.setHSL(0.08 + Math.random() * 0.05, 0.38, 0.82 + Math.random() * 0.16);
        } else {
          color.setHSL(0.55 + Math.random() * 0.1, 0.22 + Math.random() * 0.3, 0.82 + Math.random() * 0.17);
        }
        colors[i * 3] = color.r;
        colors[i * 3 + 1] = color.g;
        colors[i * 3 + 2] = color.b;
      }

      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
      geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));

      const material = new THREE.PointsMaterial({
        size: spec.size,
        map: starTexture,
        vertexColors: true,
        transparent: true,
        opacity: spec.opacity,
        alphaTest: 0.01,
        depthWrite: false,
        sizeAttenuation: true,
        blending: THREE.AdditiveBlending
      });

      const points = new THREE.Points(geometry, material);
      points.userData.speed = 0.00018 + layerIndex * 0.00007;
      points.userData.phase = layerIndex * 1.7;
      scene.add(points);
      starLayers.push(points);
    });

    const nebulae = [
      { position: [-1350, 420, -1750], scale: 2750, color: '#5b2a86', opacity: 0.2 },
      { position: [1550, -520, -1950], scale: 2450, color: '#0b4d6b', opacity: 0.18 },
      { position: [-420, 1050, -2450], scale: 3050, color: '#6d2348', opacity: 0.12 },
      { position: [950, 850, -1250], scale: 1700, color: '#213f7a', opacity: 0.14 }
    ];

    nebulae.forEach((spec) => {
      const material = new THREE.SpriteMaterial({
        map: nebulaTexture,
        color: new THREE.Color(spec.color),
        transparent: true,
        opacity: spec.opacity,
        depthWrite: false,
        blending: THREE.AdditiveBlending
      });
      const sprite = new THREE.Sprite(material);
      sprite.position.set(...spec.position);
      sprite.scale.set(spec.scale, spec.scale, 1);
      scene.add(sprite);
    });
  }

  // ---------- focus + panel ----------

  const panelTitle = $('panel-title');
  const panelGroup = $('panel-group');
  const panelDot = $('panel-dot');
  const panelPath = $('panel-path');
  const panelExcerpt = $('panel-excerpt');
  const panelDegree = $('panel-degree');
  const panelLinks = $('panel-links');

  function setNodeState(node, active, selected) {
    const viz = node.__viz;
    if (!viz) return;
    viz.active = active;
    viz.selected = selected;
    viz.halo.material.opacity = active ? (selected ? 1 : 0.78) : 0.045;
    viz.core.material.opacity = active ? 1 : 0.07;
    viz.label.material.opacity = active ? (selected ? 1 : 0.68) : 0.05;
    viz.ring.material.opacity = selected ? 0.68 : 0;
    if (!selected) {
      viz.halo.scale.set(viz.size, viz.size, 1);
      viz.core.scale.set(viz.coreSize, viz.coreSize, 1);
    }
  }

  function refreshLinks() {
    graph.linkColor(linkColor)
      .linkWidth(linkWidth)
      .linkDirectionalParticles((link) => link.__particles)
      .linkDirectionalParticleColor(() => (
        focusedNode
          ? colorFor(focusedNode.group)
          : (clusterTint || '#d6e1f2')
      ));
  }

  const positionByPath = new Map();

  function pulseNode(node) {
    if (!node || !node.__viz) return;
    node.__viz.pulseUntil = performance.now() + 1500;
  }

  function replaceGraph(payload, focusPath = '') {
    if (!payload || !Array.isArray(payload.nodes) || !payload.nodes.length) return null;

    // Keep every known star exactly where it is; only new captures move.
    positionByPath.clear();
    nodes.forEach((node) => {
      if (Number.isFinite(node.x)) {
        positionByPath.set(node.path, { x: node.x, y: node.y, z: node.z });
      }
    });

    const nextNodes = payload.nodes.map((node) => ({ ...node }));
    nextNodes.forEach((node) => {
      const previous = positionByPath.get(node.path);
      if (previous) {
        node.x = previous.x;
        node.y = previous.y;
        node.z = previous.z;
      }
    });

    const rawLinks = Array.isArray(payload.links) ? payload.links : [];
    const pairs = rawLinks.map((link) => ({
      source: Number(link.source),
      target: Number(link.target)
    }));

    // A brand-new star appears beside the neighbour it already links to.
    nextNodes.forEach((node) => {
      if (Number.isFinite(node.x)) return;
      const pair = pairs.find((item) => item.source === node.id || item.target === node.id);
      const otherId = pair ? (pair.source === node.id ? pair.target : pair.source) : -1;
      const other = otherId >= 0 ? nextNodes[otherId] : null;
      if (other && Number.isFinite(other.x)) {
        node.x = other.x + (Math.random() - 0.5) * 64;
        node.y = other.y + (Math.random() - 0.5) * 64;
        node.z = other.z + (Math.random() - 0.5) * 64;
      } else {
        const angle = Math.random() * Math.PI * 2;
        const radius = 120 + Math.random() * 160;
        node.x = Math.cos(angle) * radius;
        node.y = (Math.random() - 0.5) * 180;
        node.z = Math.sin(angle) * radius;
      }
    });

    focusedNode = null;
    hoveredNode = null;
    clusterIds = null;
    clusterTint = null;
    if (cameraFlight) {
      cameraFlight = null;
      if (controls) controls.enabled = true;
    }

    nodes.length = 0;
    nodes.push(...nextNodes);

    links.length = 0;
    rawLinks.forEach((raw, index) => {
      links.push({
        ...raw,
        source: pairs[index].source,
        target: pairs[index].target,
        __active: true,
        __particles: 0
      });
    });

    rebuildRelations();
    rebuildLegend();
    updateStats();

    graph.graphData({ nodes, links });

    if (!labelsVisible) {
      nodes.forEach((node) => {
        if (node.__viz) node.__viz.label.visible = false;
      });
    }

    const target = focusPath
      ? nodes.find((node) => node.path === focusPath)
      : null;
    if (target) {
      window.setTimeout(() => {
        focusNode(target);
        pulseNode(target);
      }, 500);
    }
    return target;
  }

  function focusNode(node, updateHash = true, showDetails = true) {
    if (!node) return;
    stopDrift();
    clearTimeout(driftTimer);
    focusedNode = node;
    clusterIds = null;
    clusterTint = null;

    const near = adjacency.get(node.id) || new Set();
    nodes.forEach((candidate) => {
      setNodeState(candidate, candidate.id === node.id || near.has(candidate.id), candidate.id === node.id);
    });

    links.forEach((link) => {
      const active = endpointId(link.source) === node.id || endpointId(link.target) === node.id;
      link.__active = active;
      link.__particles = active && !reduceMotion ? 2 : 0;
    });
    refreshLinks();
    if (showDetails) showPanel(node);
    else hidePanel();
    flyTo(node);

    if (updateHash) history.replaceState(null, '', `#note-${node.id}`);
  }

  function startCameraFlight(toPosition, toTarget, duration = 1600, onComplete = null) {
    if (!graph || !controls) return;
    stopDrift();
    const camera = graph.cameraPosition();
    const fromPosition = { x: camera.x, y: camera.y, z: camera.z };
    const fromTarget = { x: controls.target.x, y: controls.target.y, z: controls.target.z };

    if (reduceMotion || duration <= 1) {
      graph.cameraPosition(toPosition, toTarget, 0);
      controls.target.set(toTarget.x, toTarget.y, toTarget.z);
      controls.update();
      if (onComplete) onComplete();
      return;
    }

    controls.enabled = false;
    cameraFlight = {
      fromPosition,
      toPosition,
      fromTarget,
      toTarget,
      start: performance.now(),
      duration,
      onComplete
    };
  }

  function flyTo(node) {
    const camera = graph.cameraPosition();
    let dx = camera.x - node.x;
    let dy = camera.y - node.y;
    let dz = camera.z - node.z;
    let length = Math.hypot(dx, dy, dz);
    if (!length || length < 0.001) { dx = 0.3; dy = 0.2; dz = 1; length = Math.hypot(dx, dy, dz); }

    const distance = 212;
    const destination = {
      x: node.x + (dx / length) * distance,
      y: node.y + (dy / length) * distance + 16,
      z: node.z + (dz / length) * distance
    };
    startCameraFlight(destination, { x: node.x, y: node.y, z: node.z }, 1900);
  }

  function boundsForNodes(list) {
    let minX = Infinity, minY = Infinity, minZ = Infinity;
    let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
    list.forEach((node) => {
      minX = Math.min(minX, node.x); maxX = Math.max(maxX, node.x);
      minY = Math.min(minY, node.y); maxY = Math.max(maxY, node.y);
      minZ = Math.min(minZ, node.z); maxZ = Math.max(maxZ, node.z);
    });
    const center = new THREE.Vector3((minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2);
    const radius = Math.max(maxX - minX, maxY - minY, maxZ - minZ) / 2;
    return { center, radius: radius || 90 };
  }

  function flyToNodes(list, duration = 1800) {
    if (!graph || !controls || !list.length) return;
    const { center, radius } = boundsForNodes(list);
    const distance = Math.max(260, radius * 3.2 + 130);
    const direction = new THREE.Vector3(0.32, 0.2, 1).normalize();
    const position = center.clone().addScaledVector(direction, distance);
    startCameraFlight(
      { x: position.x, y: position.y, z: position.z },
      { x: center.x, y: center.y, z: center.z },
      duration
    );
  }

  function highlightCluster(list) {
    if (!list.length) return;
    stopDrift();
    clearTimeout(driftTimer);
    focusedNode = null;
    clusterIds = new Set(list.map((node) => node.id));
    clusterTint = colorFor(list[0].group);

    nodes.forEach((node) => {
      setNodeState(node, clusterIds.has(node.id), false);
    });
    links.forEach((link) => {
      const active = clusterIds.has(endpointId(link.source)) || clusterIds.has(endpointId(link.target));
      link.__active = active;
      link.__particles = active && !reduceMotion ? 1 : 0;
    });
    refreshLinks();
    hidePanel();
    history.replaceState(null, '', location.pathname + location.search);
    flyToNodes(list);
  }

  function viewBounds() {
    let minX = Infinity, minY = Infinity, minZ = Infinity;
    let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
    nodes.forEach((node) => {
      minX = Math.min(minX, node.x); maxX = Math.max(maxX, node.x);
      minY = Math.min(minY, node.y); maxY = Math.max(maxY, node.y);
      minZ = Math.min(minZ, node.z); maxZ = Math.max(maxZ, node.z);
    });
    const center = new THREE.Vector3((minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2);
    const radius = Math.max(maxX - minX, maxY - minY, maxZ - minZ) / 2;
    return { center, radius: radius || 140 };
  }

  function homeView(duration = 0) {
    if (!controls || !graph) return;
    const { center, radius } = viewBounds();
    const distance = Math.max(330, radius * 2.65 + 80);
    const direction = new THREE.Vector3(0.32, 0.2, 1).normalize();
    const position = center.clone().addScaledVector(direction, distance);
    startCameraFlight(
      { x: position.x, y: position.y, z: position.z },
      { x: center.x, y: center.y, z: center.z },
      duration
    );
  }

  function formatCreated(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleDateString('en-GB', {
      day: 'numeric',
      month: 'short',
      year: 'numeric'
    });
  }

  function showPanel(node) {
    const color = colorFor(node.group);
    panelGroup.textContent = node.group;
    panelDot.style.background = color;
    panelDot.style.color = color;
    panelTitle.textContent = node.label;
    const stamp = formatCreated(node.created);
    panelPath.textContent = stamp
      ? `notes/${node.path} · ${stamp}`
      : `notes/${node.path}`;
    panelExcerpt.textContent = node.excerpt;

    const near = [...(adjacency.get(node.id) || [])]
      .map((id) => nodeByIndex[id])
      .filter(Boolean)
      .sort((a, b) => a.group.localeCompare(b.group) || a.label.localeCompare(b.label));

    panelDegree.textContent = `${near.length} ${near.length === 1 ? 'connection' : 'connections'}`;
    panelLinks.replaceChildren();

    const fragment = document.createDocumentFragment();
    near.forEach((neighbor) => {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'connection';
      button.title = `Fly to ${neighbor.label}`;

      const dot = document.createElement('span');
      dot.className = 'connection-dot';
      dot.style.background = colorFor(neighbor.group);
      dot.style.color = colorFor(neighbor.group);

      const name = document.createElement('span');
      name.className = 'connection-name';
      name.textContent = neighbor.label;

      const meta = document.createElement('span');
      meta.className = 'connection-meta';
      const kind = pairKind(node.id, neighbor.id);
      meta.textContent = kind === 'wikilink' ? 'wikilink' : 'mention';

      const arrow = document.createElement('span');
      arrow.className = 'connection-arrow';
      arrow.setAttribute('aria-hidden', 'true');
      arrow.textContent = '→';

      button.append(dot, name, meta, arrow);
      button.addEventListener('click', () => focusNode(neighbor));
      item.append(button);
      fragment.append(item);
    });
    panelLinks.append(fragment);

    panelScroll.scrollTop = 0;
    panel.classList.add('open');
    panel.setAttribute('aria-hidden', 'false');
  }

  function hidePanel() {
    panel.classList.remove('open');
    panel.setAttribute('aria-hidden', 'true');
  }

  function clearFocus(options = {}) {
    const { close = true, resume = true, clearHash = true } = options;
    focusedNode = null;
    clusterIds = null;
    clusterTint = null;
    nodes.forEach((node) => setNodeState(node, true, false));
    links.forEach((link) => {
      link.__active = true;
      link.__particles = 0;
    });
    refreshLinks();
    if (close) hidePanel();
    if (clearHash) history.replaceState(null, '', location.pathname + location.search);
    if (resume) scheduleDrift(1400);
  }

  function dismissFocus() {
    clearFocus({ resume: false });
    homeView(1500);
    scheduleDrift(2100);
  }

  // ---------- idle drift ----------

  function startDrift() {
    if (reduceMotion || focusedNode || panel.classList.contains('open') || !controls) {
      driftWanted = false;
      return;
    }
    driftWanted = true;
    controls.autoRotate = true;
  }

  function stopDrift() {
    driftWanted = false;
    if (controls) controls.autoRotate = false;
  }

  function scheduleDrift(delay = 1600) {
    clearTimeout(driftTimer);
    driftTimer = window.setTimeout(startDrift, delay);
  }

  // ---------- camera intro ----------

  let introStarted = false;

  function beginIntro() {
    if (introStarted) return;
    introStarted = true;
    homeView(reduceMotion ? 1 : 1900);
    window.setTimeout(() => {
      loader.classList.add('done');
      window.setTimeout(() => loader.classList.add('removed'), 1300);
      scheduleDrift(1500);
    }, reduceMotion ? 250 : 2000);
    window.setTimeout(applyDeepLink, reduceMotion ? 120 : 3500);
  }

  function applyDeepLink() {
    const match = location.hash.match(/^#note-(\d+)$/);
    if (!match) return;
    const id = Number(match[1]);
    const node = nodeByIndex[id];
    if (node && node.id === id) focusNode(node, false);
  }

  // ---------- UI events ----------

  $('panel-close').addEventListener('click', dismissFocus);
  $('reset-view').addEventListener('click', () => {
    clearFocus({ resume: false });
    stopDrift();
    homeView(reduceMotion ? 1 : 1800);
    scheduleDrift(2200);
  });
  const labelsButton = $('toggle-labels');
  labelsButton.addEventListener('click', () => {
    labelsVisible = !labelsVisible;
    nodes.forEach((node) => {
      if (node.__viz) node.__viz.label.visible = labelsVisible;
    });
    labelsButton.textContent = labelsVisible ? 'Labels on' : 'Labels off';
    labelsButton.classList.toggle('active', labelsVisible);
    labelsButton.setAttribute('aria-pressed', String(labelsVisible));
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      if (panel.classList.contains('open')) dismissFocus();
      else if (clusterIds) clearFocus();
    } else if ((event.key === 'r' || event.key === 'R') && !event.metaKey && !event.ctrlKey && !event.target.matches('input, textarea')) {
      $('reset-view').click();
    }
  });

  window.addEventListener('hashchange', () => {
    if (location.hash) applyDeepLink();
  });

  // ---------- A.S.H.R.A / chat ----------

  const CHAT_HINT = 'Ask A.S.H.R.A · Say “remember that …” · Click a star';
  const chatForm = $('chat-form');
  const chatInput = $('chat-input');
  const chatSend = $('chat-send');
  const chatCard = $('chat-card');
  const chatAnswer = $('chat-answer');
  const chatSources = $('chat-sources');
  const chatStatus = $('chat-status');
  const chatMic = $('chat-mic');

  function readSessionId() {
    try {
      return sessionStorage.getItem('ashra-session') || '';
    } catch (error) {
      return '';
    }
  }

  function writeSessionId(value) {
    try {
      sessionStorage.setItem('ashra-session', value);
    } catch (error) {
      /* Session storage is optional; the server can still issue a new id. */
    }
  }

  function makeSessionId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    return `sess-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
  }

  let chatSessionId = readSessionId();

  function setChatStatus(text, kind = '') {
    if (!chatStatus) return;
    chatStatus.textContent = text;
    chatStatus.className = `chat-status${kind ? ` ${kind}` : ''}`;
  }

  function setChatBusy(busy) {
    if (chatSend) chatSend.disabled = busy;
    if (chatInput) chatInput.disabled = busy;
    if (chatMic) chatMic.disabled = busy;
    if (chatForm) {
      chatForm.classList.toggle('busy', busy);
      chatForm.setAttribute('aria-busy', String(busy));
    }
    if (busy && listening) stopListening();
  }

  function renderSources(sources) {
    if (!chatSources) return;
    chatSources.replaceChildren();

    if (typeof sources === 'string') {
      chatSources.textContent = sources;
      return;
    }

    const items = Array.isArray(sources)
      ? sources.filter((source) => source && typeof source === 'object')
      : [];
    if (!items.length) {
      chatSources.textContent = 'No matching notes';
      return;
    }

    const label = document.createElement('span');
    label.className = 'chat-sources-label';
    label.textContent = 'Sources';
    chatSources.append(label);

    items.forEach((source) => {
      const id = Number(source.id);
      const node = nodeByIndex[id];
      const name = source.label || source.title || (Number.isFinite(id) ? `Note ${id}` : 'Note');

      if (!node) {
        const span = document.createElement('span');
        span.className = 'chat-source';
        span.textContent = name;
        chatSources.append(span);
        return;
      }

      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'chat-source';
      button.textContent = name;
      button.title = `Fly to ${node.label}`;
      button.addEventListener('click', () => focusNode(node));
      chatSources.append(button);
    });
  }

  function showChatCard(text, sources, isError = false) {
    if (!chatCard || !chatAnswer || !chatSources) return;
    chatAnswer.textContent = text;
    renderSources(sources);
    chatCard.classList.toggle('error', isError);
    chatCard.hidden = false;
  }

  function beginChatCard() {
    if (!chatCard || !chatAnswer || !chatSources) return;
    chatAnswer.textContent = '';
    chatSources.replaceChildren();
    chatCard.classList.remove('error');
    chatCard.hidden = false;
  }

  function appendChatAnswer(text) {
    if (!chatAnswer || !text) return;
    chatAnswer.textContent += text;
  }

  function parseSseBlock(raw) {
    const lines = String(raw || '').split(/\r?\n/);
    let name = 'message';
    const data = [];

    for (const line of lines) {
      if (line.startsWith('event:')) {
        name = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        data.push(line.slice(5).trim());
      }
    }

    if (!data.length) return null;
    try {
      return Object.assign({ event: name }, JSON.parse(data.join('\n')));
    } catch (error) {
      return null;
    }
  }

  function revealSources(ids) {
    const seen = new Set();
    const list = [];

    (Array.isArray(ids) ? ids : []).forEach((id) => {
      const numeric = Number(id);
      if (!Number.isFinite(numeric) || seen.has(numeric)) return;
      const node = nodeByIndex[numeric];
      if (!node || node.id !== numeric) return;
      seen.add(numeric);
      list.push(node);
    });

    if (!list.length) return;

    if (list.length >= 4) {
      highlightCluster(list);
      return;
    }

    // On phones the note panel covers most of the screen, so fly and
    // highlight without stealing the answer card.
    const narrow = window.matchMedia('(max-width: 640px)').matches;
    focusNode(list[0], true, !narrow);
  }

  // ---------- voice ----------

  const synth = window.speechSynthesis || null;
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition || null;

  // Chrome exposes several British voices depending on platform. Prefer the
  // natural-sounding ones, then fall back to any en-GB voice, then any English.
  const VOICE_PREFERENCE = [
    'Google UK English Male',
    'Google UK English Female',
    'Microsoft Ryan',
    'Microsoft Thomas',
    'Microsoft Sonia',
    'Microsoft Libby',
    'Daniel',
    'Serena',
    'Kate',
    'Arthur'
  ];

  let preferredVoice = null;
  let speechPrimed = false;
  let speechToken = 0;
  let speechChunksLeft = 0;
  let speechWatch = 0;
  let listening = false;
  let recognition = null;
  let lastTranscript = '';

  function refreshVoices() {
    if (!synth || typeof synth.getVoices !== 'function') return;
    const voices = synth.getVoices() || [];
    if (!voices.length) return;

    const english = voices.filter((voice) => /^en(?:[-_]|$)/i.test(voice.lang || ''));
    const pool = english.length ? english : voices;
    const british = pool.filter((voice) => /en[-_]GB/i.test(voice.lang || ''));
    const candidates = british.length ? british : pool;

    for (const favourite of VOICE_PREFERENCE) {
      const match = candidates.find((voice) =>
        String(voice.name || '').toLowerCase().includes(favourite.toLowerCase())
      );
      if (match) {
        preferredVoice = match;
        return;
      }
    }
    preferredVoice = candidates.find((voice) => /female|male/i.test(voice.name || '')) || candidates[0] || null;
  }

  if (synth) {
    refreshVoices();
    if (typeof synth.addEventListener === 'function') {
      synth.addEventListener('voiceschanged', refreshVoices);
    } else {
      synth.onvoiceschanged = refreshVoices;
    }
  }

  function primeSpeech() {
    if (!synth || speechPrimed) return;
    speechPrimed = true;
    try {
      // A silent utterance inside the first user gesture unlocks audio for the
      // answers that arrive later from async requests.
      const warm = new SpeechSynthesisUtterance(' ');
      warm.volume = 0;
      warm.rate = 2;
      if (preferredVoice) {
        warm.voice = preferredVoice;
        warm.lang = preferredVoice.lang || 'en-GB';
      }
      synth.speak(warm);
    } catch (error) {
      /* Speech is optional. */
    }
  }

  function toSpeechText(text) {
    return String(text || '')
      .replace(/```[\s\S]*?```/g, ' Code block. ')
      .replace(/`([^`]+)`/g, '$1')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/https?:\/\/\S+/g, ' link ')
      .replace(/[*_#>~|]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function splitSpeech(text, max = 200) {
    const sentences = text.match(/[^.!?…]+[.!?…]?/g) || [text];
    const chunks = [];
    let current = '';

    const push = (value) => {
      const trimmed = value.trim();
      if (trimmed) chunks.push(trimmed);
    };

    sentences.forEach((raw) => {
      const sentence = raw.trim();
      if (!sentence) return;
      if (!current) {
        current = sentence;
      } else if (`${current} ${sentence}`.length <= max) {
        current = `${current} ${sentence}`;
      } else {
        push(current);
        current = sentence;
      }

      while (current.length > max) {
        const cut = current.lastIndexOf(' ', max);
        const index = cut > 60 ? cut : max;
        push(current.slice(0, index));
        current = current.slice(index).trim();
      }
    });

    push(current);
    return chunks;
  }

  function finishSpeaking() {
    window.clearInterval(speechWatch);
    if (listening) return;
    if (chatStatus && chatStatus.classList.contains('error')) return;
    setChatStatus(CHAT_HINT);
  }

  function stopSpeaking() {
    speechToken += 1;
    speechChunksLeft = 0;
    window.clearInterval(speechWatch);
    if (!synth) return;
    try {
      synth.cancel();
    } catch (error) {
      /* Nothing to cancel. */
    }
  }

  function speakAnswer(text) {
    if (!synth) return;
    const clean = toSpeechText(text);
    if (!clean) return;

    stopSpeaking();
    refreshVoices();
    if (synth.paused && typeof synth.resume === 'function') {
      try { synth.resume(); } catch (error) { /* The next speak starts a new queue. */ }
    }

    const chunks = splitSpeech(clean);
    if (!chunks.length) return;

    const token = speechToken;
    speechChunksLeft = chunks.length;
    setChatStatus('● speaking…', 'speaking');

    // Chrome occasionally pauses long utterances. Keep the queue alive and use
    // a generous deadline so a stuck engine cannot leave "speaking…" forever.
    window.clearInterval(speechWatch);
    const deadline = Date.now() + Math.min(180000, 6000 + clean.length * 120);
    speechWatch = window.setInterval(() => {
      if (token !== speechToken) {
        window.clearInterval(speechWatch);
        return;
      }
      if (Date.now() > deadline) {
        speechChunksLeft = 0;
        finishSpeaking();
        return;
      }
      if (synth.speaking && synth.paused) {
        try { synth.resume(); } catch (error) { /* Ignore and let the deadline fire. */ }
      }
    }, 4000);

    chunks.forEach((chunk) => {
      const utterance = new SpeechSynthesisUtterance(chunk);
      if (preferredVoice) {
        utterance.voice = preferredVoice;
        utterance.lang = preferredVoice.lang || 'en-GB';
      } else {
        utterance.lang = 'en-GB';
      }
      utterance.rate = 1.02;
      utterance.pitch = 0.98;
      utterance.volume = 1;

      utterance.onend = () => {
        if (token !== speechToken) return;
        speechChunksLeft -= 1;
        if (speechChunksLeft <= 0) finishSpeaking();
      };
      utterance.onerror = () => {
        if (token !== speechToken) return;
        speechChunksLeft = 0;
        finishSpeaking();
      };

      // A tiny gap after cancel() avoids a Chrome race where the fresh
      // utterance is dropped while the previous queue is still shutting down.
      window.setTimeout(() => {
        if (token !== speechToken) return;
        try {
          synth.speak(utterance);
        } catch (error) {
          speechChunksLeft = 0;
          finishSpeaking();
        }
      }, 60);
    });
  }

  function setListening(on) {
    listening = on;
    if (chatMic) {
      chatMic.classList.toggle('listening', on);
      chatMic.setAttribute('aria-pressed', String(on));
    }
    if (chatForm) chatForm.classList.toggle('listening', on);
  }

  function stopListening() {
    if (!listening || !recognition) return;
    try {
      recognition.stop();
    } catch (error) {
      setListening(false);
    }
  }

  function makeRecognition() {
    const rec = new Recognition();
    rec.lang = 'en-GB';
    rec.continuous = false;
    rec.interimResults = true;
    rec.maxAlternatives = 1;

    rec.onstart = () => {
      setListening(true);
      setChatStatus('● listening…', 'listening');
    };

    rec.onresult = (event) => {
      let interim = '';
      let finalText = '';

      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        if (!result || !result.length) continue;
        const transcript = String(result[0].transcript || '');
        if (result.isFinal) finalText += transcript;
        else interim += transcript;
      }

      const preview = (finalText || interim).trim();
      if (preview) lastTranscript = preview;
      setChatStatus(preview ? `● listening… ${preview}` : '● listening…', 'listening');

      if (finalText.trim()) {
        lastTranscript = '';
        try { rec.stop(); } catch (error) { /* onend still fires. */ }
        if (chatInput) chatInput.value = finalText.trim();
        askAshra(finalText.trim(), { focus: false });
      }
    };

    rec.onerror = (event) => {
      const code = (event && event.error) || '';
      if (code === 'aborted') return;

      const messages = {
        'not-allowed': '● microphone blocked — allow access in Chrome',
        'service-not-allowed': '● microphone blocked — allow access in Chrome',
        'no-speech': '● nothing heard — try again',
        'audio-capture': '● no microphone found',
        network: '● voice input needs a network connection'
      };

      setListening(false);
      setChatStatus(messages[code] || '● voice input failed', 'error');
    };

    rec.onend = () => {
      setListening(false);
      if (chatForm && chatForm.classList.contains('busy')) return;
      if (chatStatus && chatStatus.classList.contains('error')) return;

      if (lastTranscript) {
        const question = lastTranscript.trim();
        lastTranscript = '';
        if (question) {
          if (chatInput) chatInput.value = question;
          askAshra(question, { focus: false });
          return;
        }
      }
      setChatStatus(CHAT_HINT);
    };

    return rec;
  }

  function startListening() {
    if (!chatForm || !chatInput) return;
    if (!Recognition) {
      setChatStatus('● voice input needs Chrome or Edge', 'error');
      return;
    }
    if (listening) return;

    primeSpeech();
    stopSpeaking();
    if (!recognition) recognition = makeRecognition();
    lastTranscript = '';

    try {
      recognition.start();
    } catch (error) {
      setListening(false);
      setChatStatus('● voice input is already active', 'error');
    }
  }

  let greetingPlayed = false;

  function bootGreetingText() {
    const hour = new Date().getHours();
    const phase = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
    const count = nodes.length;
    return `${phase}, sir. ${count} ${count === 1 ? 'note' : 'notes'} indexed, all present and accounted for.`;
  }

  function playBootGreeting() {
    if (greetingPlayed) return;
    greetingPlayed = true;

    const attempt = () => {
      if (listening) return;
      if (chatForm && chatForm.classList.contains('busy')) return;
      if (!loader.classList.contains('done')) {
        window.setTimeout(attempt, 600);
        return;
      }
      speakAnswer(bootGreetingText());
    };

    window.setTimeout(attempt, 350);
  }

  function firstInteraction(event) {
    primeSpeech();
    const target = event && event.target;
    if (target && typeof target.closest === 'function' && target.closest('#chat-mic')) {
      // Never talk over someone who has gone straight to the microphone.
      greetingPlayed = true;
      return;
    }
    playBootGreeting();
  }

  function speakOnNextGesture() {
    document.addEventListener('pointerdown', firstInteraction, { once: true });
    document.addEventListener('keydown', firstInteraction, { once: true });
  }
  speakOnNextGesture();

  // ---------- capture ("remember that …") ----------

  const CAPTURE_PREFIXES = [
    /^(?:a\.?s\.?h\.?r\.?a[,\s]+)?(?:please\s+)?remember(?:\s+that)?[:\s]+/i,
    /^(?:a\.?s\.?h\.?r\.?a[,\s]+)?(?:please\s+)?(?:make|write|take|save)\s+(?:a\s+)?note(?:\s+that)?[:\s]+/i,
    /^(?:a\.?s\.?h\.?r\.?a[,\s]+)?(?:please\s+)?note\s+that[:\s]+/i,
    /^(?:a\.?s\.?h\.?r\.?a[,\s]+)?(?:please\s+)?(?:save|jot\s+down|log)(?:\s+that)?[:\s]+/i
  ];

  const QUESTION_START = /^(?:what|how|why|when|where|who|whom|which|do|does|did|is|are|was|were|can|could|should|would|will|have|has)\b/i;

  function captureFrom(text) {
    const raw = String(text || '').trim();
    if (!raw || /\?\s*$/.test(raw)) return null;

    for (const pattern of CAPTURE_PREFIXES) {
      const match = raw.match(pattern);
      if (!match) continue;
      const body = raw.slice(match[0].length).trim();
      if (body.length < 3 || QUESTION_START.test(body)) return null;
      return body;
    }
    return null;
  }

  async function rememberCapture(text) {
    const cleaned = String(text || '').trim();
    if (!cleaned) return;

    stopSpeaking();
    primeSpeech();
    setChatBusy(true);
    setChatStatus('● remembering…', 'busy');

    try {
      const response = await fetch('/remember', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: cleaned })
      });

      let data = null;
      try {
        data = await response.json();
      } catch (error) {
        data = null;
      }

      if (!response.ok) {
        const detail = data && (data.detail || data.error);
        throw new Error(detail || `The vault returned HTTP ${response.status}.`);
      }
      if (!data || typeof data.title !== 'string' || !data.graph) {
        throw new Error('The vault returned an unreadable response.');
      }

      replaceGraph(data.graph, data.path);

      const sources = [];
      if (typeof data.node === 'number') {
        sources.push({ id: data.node, label: data.title });
      }
      if (data.related && typeof data.related.id === 'number') {
        sources.push({
          id: data.related.id,
          label: data.related.label || data.related.title || 'Related note'
        });
      }

      const reply = typeof data.reply === 'string' && data.reply
        ? data.reply
        : `Noted, sir. "${data.title}" is in the vault.`;

      showChatCard(reply, sources, false);
      setChatStatus(CHAT_HINT);
      speakAnswer(reply);
    } catch (error) {
      stopSpeaking();
      showChatCard(
        error.message || 'The vault could not save that.',
        'Check the server log.',
        true
      );
      setChatStatus('● vault offline', 'error');
    } finally {
      setChatBusy(false);
    }
  }

  async function streamAnswer(question) {
    const response = await fetch('/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question,
        session_id: chatSessionId || null
      })
    });

    if (!response.ok) {
      let data = null;
      try {
        data = await response.json();
      } catch (error) {
        data = null;
      }
      const detail = data && (data.detail || data.error);
      throw new Error(detail || `A.S.H.R.A returned HTTP ${response.status}.`);
    }

    if (!response.body || typeof response.body.getReader !== 'function') {
      return null;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let answer = '';
    let sessionId = '';
    let nodes = [];
    let sources = [];
    let sawDone = false;

    beginChatCard();

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let boundary = buffer.indexOf('\n\n');
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = parseSseBlock(block);

        if (event) {
          if (event.event === 'start') {
            if (event.session_id) {
              sessionId = event.session_id;
              chatSessionId = sessionId;
              writeSessionId(sessionId);
            }
          } else if (event.event === 'sources') {
            nodes = Array.isArray(event.nodes) ? event.nodes : [];
            sources = Array.isArray(event.sources) ? event.sources : [];
            renderSources(sources);
            revealSources(nodes);
          } else if (event.event === 'token') {
            const text = typeof event.text === 'string' ? event.text : '';
            answer += text;
            appendChatAnswer(text);
          } else if (event.event === 'done') {
            sawDone = true;
            if (event.session_id) {
              sessionId = event.session_id;
              chatSessionId = sessionId;
              writeSessionId(sessionId);
            }
            if (typeof event.answer === 'string' && event.answer) {
              answer = event.answer;
              if (chatAnswer) chatAnswer.textContent = answer;
            }
            if (Array.isArray(event.nodes)) nodes = event.nodes;
            if (Array.isArray(event.sources)) sources = event.sources;
            renderSources(sources);
            revealSources(nodes);
          } else if (event.event === 'error') {
            throw new Error(event.detail || event.error || 'A.S.H.R.A could not answer.');
          }
        }

        boundary = buffer.indexOf('\n\n');
      }
    }

    if (!sawDone && !answer) {
      throw new Error('A.S.H.R.A returned an empty stream.');
    }

    return {
      answer,
      session_id: sessionId,
      nodes,
      sources
    };
  }

  async function askAshra(question, options = {}) {
    const cleaned = String(question || '').trim();
    if (!cleaned || !chatForm) return;

    const capture = captureFrom(cleaned);
    if (capture) {
      await rememberCapture(capture);
      if (chatInput && options.focus !== false) chatInput.focus();
      return;
    }

    stopSpeaking();
    primeSpeech();
    setChatBusy(true);
    setChatStatus('● thinking…', 'busy');

    try {
      const streamed = await streamAnswer(cleaned);
      if (streamed) {
        setChatStatus(CHAT_HINT);
        speakAnswer(streamed.answer);
        return;
      }

      // Fallback for a browser or proxy that does not expose a stream body.
      const response = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: cleaned,
          session_id: chatSessionId || null
        })
      });

      let data = null;
      try {
        data = await response.json();
      } catch (error) {
        data = null;
      }

      if (!response.ok) {
        const detail = data && (data.detail || data.error);
        throw new Error(detail || `A.S.H.R.A returned HTTP ${response.status}.`);
      }
      if (!data || typeof data.answer !== 'string') {
        throw new Error('A.S.H.R.A returned an unreadable response.');
      }

      if (data.session_id) {
        chatSessionId = data.session_id;
        writeSessionId(chatSessionId);
      }

      showChatCard(data.answer, data.sources, false);
      revealSources(data.nodes);
      setChatStatus(CHAT_HINT);
      speakAnswer(data.answer);
    } catch (error) {
      stopSpeaking();
      showChatCard(
        error.message || 'A.S.H.R.A could not answer.',
        'Check config.json and the server log.',
        true
      );
      setChatStatus('● A.S.H.R.A offline', 'error');
    } finally {
      setChatBusy(false);
      if (chatInput && options.focus !== false) chatInput.focus();
    }
  }

  if (chatForm && chatInput && chatSend) {
    if (!chatSessionId) {
      chatSessionId = makeSessionId();
      writeSessionId(chatSessionId);
    }

    chatForm.addEventListener('submit', (event) => {
      event.preventDefault();
      const question = chatInput.value.trim();
      if (!question) {
        chatInput.focus();
        return;
      }
      chatInput.value = '';
      askAshra(question);
    });
  }

  if (chatMic) {
    if (!Recognition) {
      chatMic.disabled = true;
      chatMic.title = 'Voice input is not supported in this browser';
      chatMic.setAttribute('aria-disabled', 'true');
    } else {
      chatMic.addEventListener('click', () => {
        primeSpeech();
        if (listening) {
          stopListening();
          if (!lastTranscript) setChatStatus(CHAT_HINT);
          return;
        }
        startListening();
      });
    }
  }

  // ---------- animation ----------

  let lastTime = 0;
  let elapsed = 0;

  function animate(now) {
    window.requestAnimationFrame(animate);
    if (!lastTime) lastTime = now;
    elapsed += Math.min((now - lastTime) / 1000, 0.05);
    lastTime = now;

    if (driftWanted && controls && !focusedNode && !panel.classList.contains('open')) {
      controls.autoRotate = true;
    }

    if (cameraFlight) {
      const flight = cameraFlight;
      const t = Math.min(1, (now - flight.start) / flight.duration);
      const eased = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
      const lerp = (a, b) => a + (b - a) * eased;
      const position = {
        x: lerp(flight.fromPosition.x, flight.toPosition.x),
        y: lerp(flight.fromPosition.y, flight.toPosition.y),
        z: lerp(flight.fromPosition.z, flight.toPosition.z)
      };
      const target = {
        x: lerp(flight.fromTarget.x, flight.toTarget.x),
        y: lerp(flight.fromTarget.y, flight.toTarget.y),
        z: lerp(flight.fromTarget.z, flight.toTarget.z)
      };
      position.y += Math.sin(Math.PI * eased) * 18;
      graph.cameraPosition(position, target, 0);

      if (t >= 1) {
        cameraFlight = null;
        if (controls) {
          controls.enabled = true;
          controls.target.set(flight.toTarget.x, flight.toTarget.y, flight.toTarget.z);
          controls.update();
        }
        if (flight.onComplete) flight.onComplete();
      }
    }

    starLayers.forEach((layer) => {
      layer.rotation.y += layer.userData.speed;
      layer.rotation.x = Math.sin(elapsed * 0.035 + layer.userData.phase) * 0.028;
    });

    nodes.forEach((node) => {
      const viz = node.__viz;
      if (!viz) return;
      let factor = 1;
      if (viz.active) factor += Math.sin(elapsed * 0.72 + node.__phase) * 0.018;
      if (viz.selected) factor *= 1.14 + Math.sin(elapsed * 2.2) * 0.06;
      if (viz.pulseUntil) {
        const remaining = viz.pulseUntil - now;
        if (remaining > 0) {
          factor *= 1 + Math.sin((1 - remaining / 1500) * Math.PI) * 0.55;
        } else {
          viz.pulseUntil = 0;
        }
      }
      if (hoveredNode === node) factor *= 1.08;
      viz.halo.scale.set(viz.size * factor, viz.size * factor, 1);
      viz.core.scale.set(viz.coreSize * factor, viz.coreSize * factor, 1);
      if (viz.selected) {
        const ringFactor = 1.86 + Math.sin(elapsed * 2.2) * 0.03;
        viz.ring.scale.set(viz.size * ringFactor, viz.size * ringFactor, 1);
      }
    });
  }

  // ---------- boot ----------

  async function boot() {
    try {
      await Promise.race([
        document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve(),
        new Promise((resolve) => window.setTimeout(resolve, 900))
      ]);
    } catch (error) {
      /* Fonts are optional. */
    }

    try {
      initGraph();
      window.requestAnimationFrame(animate);
      window.setTimeout(beginIntro, reduceMotion ? 120 : 900);
      window.setTimeout(() => beginIntro(), 4200);
    } catch (error) {
      console.error(error);
      fail(`The galaxy could not be rendered: ${error.message}`);
    }
  }

  // Exposed for debugging and for future index-based features.
  window.__ashra = {
    graph: () => graph,
    nodes,
    links,
    ask: askAshra,
    sessionId: () => chatSessionId,
    speak: speakAnswer,
    stopSpeaking,
    listen: startListening,
    stopListening,
    voice: () => (preferredVoice ? preferredVoice.name : null),
    speechSupported: Boolean(synth),
    voiceInputSupported: Boolean(Recognition),
    greeting: bootGreetingText,
    greet: playBootGreeting,
    revealSources,
    captureFrom: (text) => captureFrom(text),
    remember: rememberCapture,
    replaceGraph,
    activeCluster: () => (clusterIds ? [...clusterIds] : []),
    focusById: (id) => {
      const node = nodeByIndex[id];
      if (node && node.id === id) focusNode(node);
    },
    reset: () => $('reset-view').click(),
    confirmDelete: (path) => {
        if (!confirm('Delete the capture?')) return;
        fetch('/forget', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: path, confirm: true })
        })
            .then(r => r.json())
            .then(data => {
                if (data.ok) {
                    alert('Deleted, sir');
                    location.reload();
                } else {
                    alert(`Error: ${data.error || JSON.stringify(data)}`);
                }
            });
    }
  };

  boot();
})();
