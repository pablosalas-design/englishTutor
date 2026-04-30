// Avatar 3D con Three.js + Ready Player Me + lipsync por amplitud de audio.
// Módulo ES. Se importa desde app.js.

import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const VISEME_TARGETS = [
  // Morphs principales que vamos a animar para el lipsync.
  "jawOpen",
  "mouthOpen",
  "mouthFunnel",
  "mouthPucker",
  "mouthSmile",
  "mouthSmileLeft",
  "mouthSmileRight",
];

const BLINK_TARGETS = ["eyeBlinkLeft", "eyeBlinkRight"];

class Avatar3D {
  constructor(canvas) {
    this.canvas = canvas;
    this.scene = new THREE.Scene();
    this.scene.background = null;

    this.camera = new THREE.PerspectiveCamera(28, 1, 0.1, 100);
    this.camera.position.set(0, 1.55, 1.1);

    this.renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: true,
      alpha: true,
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;

    this._setupLights();

    this.morphMeshes = []; // [{mesh, indices: {jawOpen: i, ...}}]
    this.headBone = null;
    this.avatarRoot = null;

    // Estado de lipsync
    this.audioCtx = null;
    this.analyser = null;
    this.audioData = null;
    this.currentMouth = 0;
    this.targetMouth = 0;

    // Estado de parpadeo / idle
    this.nextBlinkAt = performance.now() + 2000 + Math.random() * 3000;
    this.blinkPhase = 0; // 0 = no, 1 = cerrando, 2 = abriendo
    this.blinkStart = 0;

    this._clock = new THREE.Clock();
    this._loop = this._loop.bind(this);
    this._running = false;

    window.addEventListener("resize", () => this._resize());
    this._resize();
  }

  _setupLights() {
    const hemi = new THREE.HemisphereLight(0xffffff, 0x222244, 1.0);
    this.scene.add(hemi);

    const key = new THREE.DirectionalLight(0xffffff, 1.4);
    key.position.set(1.5, 2, 2);
    this.scene.add(key);

    const fill = new THREE.DirectionalLight(0x99aaff, 0.5);
    fill.position.set(-1.5, 1, 1);
    this.scene.add(fill);

    const rim = new THREE.DirectionalLight(0xffaaff, 0.4);
    rim.position.set(0, 1.5, -2);
    this.scene.add(rim);
  }

  _resize() {
    const rect = this.canvas.getBoundingClientRect();
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height));
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  start() {
    if (this._running) return;
    this._running = true;
    this._clock.start();
    this._loop();
  }

  stop() {
    this._running = false;
  }

  async loadAvatar(url) {
    // Descarga el GLB y lo prepara. Devuelve cuando está listo.
    if (this.avatarRoot) {
      this.scene.remove(this.avatarRoot);
      this.avatarRoot = null;
      this.morphMeshes = [];
      this.headBone = null;
    }

    // Truco RPM: añadir parámetros para reducir peso del modelo y obtener morphs.
    const finalUrl = this._withRpmParams(url);

    const loader = new GLTFLoader();
    const gltf = await loader.loadAsync(finalUrl);
    const root = gltf.scene;
    root.traverse((obj) => {
      if (obj.isMesh) {
        obj.frustumCulled = false;
        if (obj.morphTargetDictionary && obj.morphTargetInfluences) {
          const indices = {};
          for (const name of VISEME_TARGETS.concat(BLINK_TARGETS)) {
            if (name in obj.morphTargetDictionary) {
              indices[name] = obj.morphTargetDictionary[name];
            }
          }
          if (Object.keys(indices).length > 0) {
            this.morphMeshes.push({ mesh: obj, indices });
          }
        }
      }
      if (obj.isBone && /head/i.test(obj.name) && !this.headBone) {
        this.headBone = obj;
      }
    });

    // Encuadre: el avatar de RPM viene de pie. Lo subimos para que la cámara enfoque la cabeza.
    root.position.set(0, -1.5, 0);
    this.avatarRoot = root;
    this.scene.add(root);

    return root;
  }

  _withRpmParams(url) {
    // Pedimos morphs para lipsync y blinks. Si la URL no es de RPM, la dejamos tal cual.
    if (!/readyplayer\.me/i.test(url)) return url;
    const sep = url.includes("?") ? "&" : "?";
    return `${url}${sep}morphTargets=ARKit,Oculus%20Visemes&textureAtlas=1024&pose=A&useHands=false`;
  }

  // Conecta un MediaStream (el audio remoto del WebRTC de OpenAI) al analizador.
  attachAudioStream(mediaStream) {
    try {
      if (!this.audioCtx) {
        this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      if (this.audioCtx.state === "suspended") {
        this.audioCtx.resume().catch(() => {});
      }
      const src = this.audioCtx.createMediaStreamSource(mediaStream);
      this.analyser = this.audioCtx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.analyser.smoothingTimeConstant = 0.4;
      this.audioData = new Uint8Array(this.analyser.fftSize);
      src.connect(this.analyser);
      // Workaround Chromium: enchufar a un gain con 0 fuerza al grafo a tirar audio.
      const silent = this.audioCtx.createGain();
      silent.gain.value = 0;
      this.analyser.connect(silent);
      silent.connect(this.audioCtx.destination);
    } catch (e) {
      console.warn("attachAudioStream failed", e);
    }
  }

  detachAudio() {
    this.analyser = null;
    this.audioData = null;
    this.targetMouth = 0;
    this.currentMouth = 0;
  }

  _readAudioLevel() {
    if (!this.analyser || !this.audioData) return 0;
    this.analyser.getByteTimeDomainData(this.audioData);
    let sum = 0;
    for (let i = 0; i < this.audioData.length; i++) {
      const v = (this.audioData[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / this.audioData.length);
    // Realzar amplitud: la voz hablada suele estar entre 0.02 y 0.2 de RMS.
    return Math.min(1, rms * 6);
  }

  _applyMouth(value) {
    // value: 0..1
    const jaw = value * 0.75;
    const mouth = value * 0.5;
    const funnel = Math.max(0, value - 0.4) * 0.6;
    for (const { mesh, indices } of this.morphMeshes) {
      if ("jawOpen" in indices) mesh.morphTargetInfluences[indices.jawOpen] = jaw;
      if ("mouthOpen" in indices) mesh.morphTargetInfluences[indices.mouthOpen] = mouth;
      if ("mouthFunnel" in indices) mesh.morphTargetInfluences[indices.mouthFunnel] = funnel;
    }
  }

  _applyBlink(value) {
    for (const { mesh, indices } of this.morphMeshes) {
      if ("eyeBlinkLeft" in indices) mesh.morphTargetInfluences[indices.eyeBlinkLeft] = value;
      if ("eyeBlinkRight" in indices) mesh.morphTargetInfluences[indices.eyeBlinkRight] = value;
    }
  }

  _updateBlink(now) {
    if (this.blinkPhase === 0) {
      if (now >= this.nextBlinkAt) {
        this.blinkPhase = 1;
        this.blinkStart = now;
      } else {
        this._applyBlink(0);
        return;
      }
    }
    const t = now - this.blinkStart;
    if (this.blinkPhase === 1) {
      // Cierra en 80ms
      const v = Math.min(1, t / 80);
      this._applyBlink(v);
      if (t >= 80) {
        this.blinkPhase = 2;
        this.blinkStart = now;
      }
    } else if (this.blinkPhase === 2) {
      // Abre en 120ms
      const v = Math.max(0, 1 - t / 120);
      this._applyBlink(v);
      if (t >= 120) {
        this.blinkPhase = 0;
        this.nextBlinkAt = now + 2500 + Math.random() * 3500;
      }
    }
  }

  _updateIdle(elapsed) {
    if (!this.headBone) return;
    // Sutil balanceo de la cabeza
    this.headBone.rotation.y = Math.sin(elapsed * 0.6) * 0.05;
    this.headBone.rotation.x = Math.sin(elapsed * 0.4) * 0.02;
  }

  _loop() {
    if (!this._running) return;
    requestAnimationFrame(this._loop);

    const now = performance.now();
    const elapsed = this._clock.getElapsedTime();

    // Lipsync
    const level = this._readAudioLevel();
    this.targetMouth = level;
    this.currentMouth += (this.targetMouth - this.currentMouth) * 0.45;
    this._applyMouth(this.currentMouth);

    // Blink + idle
    this._updateBlink(now);
    this._updateIdle(elapsed);

    this.renderer.render(this.scene, this.camera);
  }
}

let instance = null;

export function getAvatar(canvas) {
  if (!instance) {
    instance = new Avatar3D(canvas);
    instance.start();
  }
  return instance;
}
