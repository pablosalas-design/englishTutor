// Avatar 3D con Three.js + Ready Player Me + lipsync por amplitud de audio.
// Módulo ES. Se importa desde app.js.

import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

// Lista expandida de morphs para lipsync, cubriendo ARKit, Avaturn, RPM y Oculus visemes.
const VISEME_TARGETS = [
  "jawOpen", "JawOpen", "jaw_open",
  "mouthOpen", "MouthOpen", "mouth_open",
  "mouthFunnel", "MouthFunnel", "mouth_funnel",
  "mouthPucker", "MouthPucker", "mouth_pucker",
  "mouthSmile", "mouthSmileLeft", "mouthSmileRight",
  "mouthSmile_L", "mouthSmile_R",
  "viseme_aa", "viseme_E", "viseme_I", "viseme_O", "viseme_U",
  "viseme_PP", "viseme_FF", "viseme_TH", "viseme_DD", "viseme_kk",
  "viseme_CH", "viseme_SS", "viseme_nn", "viseme_RR", "viseme_sil",
];

const BLINK_TARGETS = [
  "eyeBlinkLeft", "eyeBlinkRight",
  "eyeBlink_L", "eyeBlink_R",
  "EyeBlinkLeft", "EyeBlinkRight",
  "eyesClosed",
];

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

    // Detener mixer anterior si lo había
    if (this.mixer) {
      this.mixer.stopAllAction();
      this.mixer = null;
    }

    const loader = new GLTFLoader();
    const gltf = await loader.loadAsync(finalUrl);
    const root = gltf.scene;

    // Si el GLB trae animaciones (Avaturn "with animation"), las reproducimos.
    if (gltf.animations && gltf.animations.length > 0) {
      this.mixer = new THREE.AnimationMixer(root);
      // Ejecutar todas las animaciones que vengan (típicamente solo una de idle)
      for (const clip of gltf.animations) {
        const action = this.mixer.clipAction(clip);
        action.play();
      }
      console.log("[avatar] playing", gltf.animations.length, "animation(s)");
    }
    const allMorphNames = new Set();
    const allBoneNames = [];
    const armBones = [];
    root.traverse((obj) => {
      if (obj.isMesh) {
        obj.frustumCulled = false;
        if (obj.morphTargetDictionary && obj.morphTargetInfluences) {
          const indices = {};
          for (const name of Object.keys(obj.morphTargetDictionary)) {
            allMorphNames.add(name);
          }
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
      if (obj.isBone) {
        allBoneNames.push(obj.name);
        if (/head/i.test(obj.name) && !this.headBone) this.headBone = obj;
        // Brazos: detectamos por nombre, evitando ForeArm/Forearm (codo) y Shoulder (clavícula)
        const isLeft = /left|_l$|\.l$|^l_/i.test(obj.name);
        const isRight = /right|_r$|\.r$|^r_/i.test(obj.name);
        const isUpperArm = /(upper.?arm|^arm|[^a-z]arm)/i.test(obj.name)
                          && !/fore.?arm/i.test(obj.name)
                          && !/shoulder|clavic/i.test(obj.name)
                          && !/hand|finger|thumb|index|middle|ring|pinky/i.test(obj.name);
        if (isUpperArm && isLeft) armBones.push({ bone: obj, side: "L" });
        else if (isUpperArm && isRight) armBones.push({ bone: obj, side: "R" });
      }
    });

    // Bajar brazos solo si el GLB no trae animación (el "with animation" ya viene en pose natural)
    if (!this.mixer) {
      for (const { bone, side } of armBones) {
        const angle = side === "L" ? 1.25 : -1.25; // ~72°
        bone.rotation.z += angle;
      }
    }

    const debugInfo = {
      animations: gltf.animations ? gltf.animations.length : 0,
      morphs: [...allMorphNames],
      mouthMatched: this.morphMeshes.map(m => Object.keys(m.indices)),
      bones: allBoneNames,
      armsRotated: armBones.map(a => a.bone.name),
    };
    console.log("[avatar] debug", debugInfo);
    const dbgEl = document.getElementById("avatarDebug");
    if (dbgEl) {
      dbgEl.textContent = JSON.stringify(debugInfo, null, 2);
      dbgEl.hidden = false;
      // Permitir cerrar tocando el panel
      dbgEl.onclick = () => { dbgEl.hidden = true; };
    }

    this.avatarRoot = root;
    this.scene.add(root);

    // Auto-encuadre: calculamos el bounding box del avatar y posicionamos
    // la cámara para enmarcar la cabeza/torso, sin importar la escala/origen.
    this._frameHead(root);

    return root;
  }

  _frameHead(root) {
    // Asegurar que las matrices están calculadas
    root.updateMatrixWorld(true);

    const box = new THREE.Box3().setFromObject(root);
    const size = new THREE.Vector3();
    const center = new THREE.Vector3();
    box.getSize(size);
    box.getCenter(center);

    // El "alto" del avatar (Y). Si es 0 (caso raro), salimos.
    const height = size.y || 1;

    // Punto objetivo: si tenemos el hueso de la cabeza, lo usamos (más fiable),
    // si no, estimamos que la cabeza está al ~92% de la altura desde el suelo.
    let headTarget;
    if (this.headBone) {
      const headPos = new THREE.Vector3();
      this.headBone.getWorldPosition(headPos);
      // Bajamos un pelín para encuadrar cara + hombros, no la coronilla
      headTarget = new THREE.Vector3(headPos.x, headPos.y - height * 0.05, headPos.z);
    } else {
      const headY = box.min.y + height * 0.90;
      headTarget = new THREE.Vector3(center.x, headY, center.z);
    }

    // Distancia: queremos que cabeza+cuello+hombros ocupen ~70% del alto del canvas.
    // Cabeza humana ≈ 13% de la altura total. Encuadre busto ≈ 25% de altura.
    const fovRad = (this.camera.fov * Math.PI) / 180;
    const frameHeight = height * 0.22; // alto que queremos que ocupe la pantalla (cara + busto)
    const distance = (frameHeight / 2) / Math.tan(fovRad / 2);

    this.camera.position.set(headTarget.x, headTarget.y, headTarget.z + distance);
    this.camera.lookAt(headTarget);
    this.camera.near = Math.max(0.01, distance / 100);
    this.camera.far = distance * 100;
    this.camera.updateProjectionMatrix();

    console.log("[avatar] framed", {
      bbox: { min: box.min.toArray(), max: box.max.toArray() },
      headBone: !!this.headBone,
      target: headTarget.toArray(),
      distance,
    });
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

  _setMorph(indices, mesh, names, value) {
    for (const n of names) {
      if (n in indices) {
        mesh.morphTargetInfluences[indices[n]] = value;
      }
    }
  }

  _applyMouth(value) {
    // value: 0..1
    const jaw = value * 0.85;
    const mouth = value * 0.6;
    const funnel = Math.max(0, value - 0.4) * 0.6;
    for (const { mesh, indices } of this.morphMeshes) {
      this._setMorph(indices, mesh, ["jawOpen", "JawOpen", "jaw_open"], jaw);
      this._setMorph(indices, mesh, ["mouthOpen", "MouthOpen", "mouth_open"], mouth);
      this._setMorph(indices, mesh, ["mouthFunnel", "MouthFunnel", "mouth_funnel"], funnel);
      // Visemas Oculus: cuando hay amplitud, abrimos vocal "aa"
      this._setMorph(indices, mesh, ["viseme_aa"], jaw);
      this._setMorph(indices, mesh, ["viseme_O"], funnel);
    }
  }

  _applyBlink(value) {
    for (const { mesh, indices } of this.morphMeshes) {
      this._setMorph(indices, mesh,
        ["eyeBlinkLeft", "eyeBlink_L", "EyeBlinkLeft"], value);
      this._setMorph(indices, mesh,
        ["eyeBlinkRight", "eyeBlink_R", "EyeBlinkRight"], value);
      this._setMorph(indices, mesh, ["eyesClosed"], value);
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
    // Solo balanceamos la cabeza manualmente si no hay animación incrustada
    if (this.mixer || !this.headBone) return;
    this.headBone.rotation.y = Math.sin(elapsed * 0.6) * 0.05;
    this.headBone.rotation.x = Math.sin(elapsed * 0.4) * 0.02;
  }

  _loop() {
    if (!this._running) return;
    requestAnimationFrame(this._loop);

    const now = performance.now();
    const delta = this._clock.getDelta();
    const elapsed = this._clock.elapsedTime;

    // Animación incrustada (idle del Avaturn "with animation")
    if (this.mixer) this.mixer.update(delta);

    // Lipsync
    const level = this._readAudioLevel();
    this.targetMouth = level;
    this.currentMouth += (this.targetMouth - this.currentMouth) * 0.45;
    this._applyMouth(this.currentMouth);

    // Blink + idle (manual solo si no hay mixer)
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
