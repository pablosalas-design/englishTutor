import { getAvatar } from "/static/avatar.js?v=10";

const REALTIME_MODEL = "gpt-4o-realtime-preview";

const state = {
  mode: null,
  pc: null,
  dc: null,
  micStream: null,
  audioEl: null,
  muted: false,
  modes: [],
  pendingUserText: "",
  pendingAssistantText: "",
  loadedAvatarUrl: null,
};

const els = {
  picker: document.getElementById("picker"),
  conversation: document.getElementById("conversation"),
  modeCards: document.getElementById("modeCards"),
  convLabel: document.getElementById("convLabel"),
  convStatus: document.getElementById("convStatus"),
  orb: document.getElementById("orb"),
  avatarCanvas: document.getElementById("avatarCanvas"),
  avatarLoader: document.getElementById("avatarLoader"),
  transcript: document.getElementById("transcript"),
  backBtn: document.getElementById("backBtn"),
  endBtn: document.getElementById("endBtn"),
  muteBtn: document.getElementById("muteBtn"),
  app: document.getElementById("app"),
};

let avatar = null;
function ensureAvatar() {
  if (!avatar) avatar = getAvatar(els.avatarCanvas);
  return avatar;
}

// ---------- Boot ----------

async function boot() {
  const r = await fetch("/api/modes");
  state.modes = await r.json();
  renderPicker();
}

function renderPicker() {
  els.modeCards.innerHTML = "";
  for (const m of state.modes) {
    const card = document.createElement("button");
    card.className = "mode-card";
    card.style.setProperty("--mc-color", m.color);
    card.innerHTML = `
      <div class="avatar">${initials(m.label)}</div>
      <div class="info">
        <div class="name">${m.label}</div>
        <div class="sub">${m.subtitle}</div>
      </div>
      <div class="arrow">→</div>
    `;
    card.addEventListener("click", () => startConversation(m));
    els.modeCards.appendChild(card);
  }
}

function initials(label) {
  return label
    .split(" ")
    .filter(Boolean)
    .slice(0, 2)
    .map(w => w[0])
    .join("")
    .toUpperCase();
}

// ---------- Conversation lifecycle ----------

async function startConversation(mode) {
  state.mode = mode;
  els.convLabel.textContent = mode.label;
  els.convStatus.textContent = "Conectando…";
  els.transcript.innerHTML = "";
  els.app.style.setProperty("--mc-color", mode.color);
  document.documentElement.style.setProperty("--mc-color", mode.color);
  showScreen("conversation");
  setOrb("idle");

  // Carga del avatar 3D si hay URL configurada (en paralelo a la conexión).
  loadAvatarFor(mode);

  try {
    await connectRealtime(mode.id);
    els.convStatus.textContent = "En vivo";
    els.endBtn.hidden = false;
    els.muteBtn.hidden = false;
  } catch (err) {
    console.error(err);
    els.convStatus.textContent = "Error de conexión";
    addBubble("assistant", "No pude conectar con la profesora. Comprueba tu conexión y vuelve a intentarlo.");
  }
}

async function loadAvatarFor(mode) {
  const url = mode.avatar_url || "";
  console.log("[app] loadAvatarFor", { mode: mode.id, url });
  if (!url) {
    // No hay avatar configurado: mostramos el orbe como respaldo
    els.orb.hidden = false;
    els.avatarCanvas.hidden = true;
    els.avatarLoader.hidden = true;
    return;
  }
  els.orb.hidden = true;
  els.avatarCanvas.hidden = false;
  if (state.loadedAvatarUrl === url) {
    els.avatarLoader.hidden = true;
    return;
  }
  els.avatarLoader.textContent = "Cargando avatar…";
  els.avatarLoader.hidden = false;
  try {
    const av = ensureAvatar();
    await av.loadAvatar(url);
    state.loadedAvatarUrl = url;
    els.avatarLoader.hidden = true;
    console.log("[app] avatar loaded OK");
  } catch (err) {
    console.error("[app] avatar load failed", err);
    els.avatarLoader.textContent = "No se pudo cargar el avatar (" + (err && err.message ? err.message : "error") + ")";
    els.avatarLoader.hidden = false;
    els.orb.hidden = false;
    els.avatarCanvas.hidden = true;
  }
}

async function connectRealtime(modeId) {
  // 1) Pedir token efímero al backend
  const tokenRes = await fetch("/api/token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode: modeId }),
  });
  if (!tokenRes.ok) throw new Error("token failed");
  const session = await tokenRes.json();
  const ephemeralKey = session.client_secret.value;

  // 2) Crear conexión WebRTC
  const pc = new RTCPeerConnection();
  state.pc = pc;

  // Reproducir audio de la profesora
  const audioEl = document.createElement("audio");
  audioEl.autoplay = true;
  state.audioEl = audioEl;
  pc.ontrack = (e) => {
    audioEl.srcObject = e.streams[0];
    // Conectamos el mismo stream al avatar para sincronizar la boca con la voz
    if (avatar) {
      try {
        avatar.attachAudioStream(e.streams[0]);
      } catch (err) {
        console.warn("avatar attach failed", err);
      }
    }
  };

  // Capturar micrófono
  const ms = await navigator.mediaDevices.getUserMedia({ audio: true });
  state.micStream = ms;
  ms.getTracks().forEach(track => pc.addTrack(track, ms));

  // Canal de datos para eventos
  const dc = pc.createDataChannel("oai-events");
  state.dc = dc;
  dc.addEventListener("message", (e) => {
    try {
      handleServerEvent(JSON.parse(e.data));
    } catch (err) {
      console.warn("non-json event", e.data);
    }
  });

  // 3) Crear oferta SDP y enviar a OpenAI
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  const sdpResponse = await fetch(`https://api.openai.com/v1/realtime?model=${REALTIME_MODEL}`, {
    method: "POST",
    body: offer.sdp,
    headers: {
      Authorization: `Bearer ${ephemeralKey}`,
      "Content-Type": "application/sdp",
    },
  });
  if (!sdpResponse.ok) throw new Error("sdp exchange failed");
  const answerSdp = await sdpResponse.text();
  await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
}

function endConversation() {
  if (state.dc) try { state.dc.close(); } catch {}
  if (state.pc) try { state.pc.close(); } catch {}
  if (state.micStream) state.micStream.getTracks().forEach(t => t.stop());
  state.dc = state.pc = state.micStream = null;
  state.muted = false;
  els.muteBtn.classList.remove("muted");
  els.muteBtn.textContent = "🎙️ Silenciar";
  setOrb("idle");
  if (avatar) avatar.detachAudio();
  showScreen("picker");
}

// ---------- Realtime events ----------

function handleServerEvent(evt) {
  switch (evt.type) {
    case "input_audio_buffer.speech_started":
      setOrb("user-speaking");
      break;
    case "input_audio_buffer.speech_stopped":
      setOrb("idle");
      break;
    case "response.audio.delta":
      setOrb("ai-speaking");
      break;
    case "response.audio.done":
      setOrb("idle");
      break;
    case "response.audio_transcript.delta":
      state.pendingAssistantText += evt.delta || "";
      updateLastBubble("assistant", state.pendingAssistantText);
      break;
    case "response.audio_transcript.done":
      if (state.pendingAssistantText.trim()) {
        saveTranscript("assistant", state.pendingAssistantText.trim());
      }
      state.pendingAssistantText = "";
      break;
    case "conversation.item.input_audio_transcription.completed":
      if (evt.transcript) {
        addBubble("user", evt.transcript);
        saveTranscript("user", evt.transcript);
      }
      break;
    case "error":
      console.error("Realtime error", evt);
      els.convStatus.textContent = "Error";
      break;
  }
}

async function saveTranscript(role, content) {
  try {
    await fetch("/api/transcript", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: state.mode.id, role, content }),
    });
  } catch (e) {
    console.warn("transcript save failed", e);
  }
}

// ---------- UI ----------

function showScreen(name) {
  for (const s of document.querySelectorAll(".screen")) s.classList.remove("active");
  document.getElementById(name).classList.add("active");
}

function setOrb(stateName) {
  els.orb.classList.remove("idle", "user-speaking", "ai-speaking");
  els.orb.classList.add(stateName);
}

function addBubble(role, text) {
  const div = document.createElement("div");
  div.className = `bubble ${role}`;
  div.dataset.role = role;
  div.textContent = text;
  els.transcript.appendChild(div);
  els.transcript.scrollTop = els.transcript.scrollHeight;
}

function updateLastBubble(role, text) {
  let last = els.transcript.lastElementChild;
  if (!last || last.dataset.role !== role || last.dataset.streaming !== "1") {
    last = document.createElement("div");
    last.className = `bubble ${role}`;
    last.dataset.role = role;
    last.dataset.streaming = "1";
    els.transcript.appendChild(last);
  }
  last.textContent = text;
  els.transcript.scrollTop = els.transcript.scrollHeight;
  if (text.endsWith("\n") || text.length > 1500) {
    last.dataset.streaming = "0";
  }
}

// ---------- Buttons ----------

els.backBtn.addEventListener("click", endConversation);
els.endBtn.addEventListener("click", endConversation);
els.muteBtn.addEventListener("click", () => {
  if (!state.micStream) return;
  state.muted = !state.muted;
  state.micStream.getAudioTracks().forEach(t => (t.enabled = !state.muted));
  els.muteBtn.classList.toggle("muted", state.muted);
  els.muteBtn.textContent = state.muted ? "🔇 Reactivar mic" : "🎙️ Silenciar";
});

// Al cerrar pestaña, liberar recursos
window.addEventListener("pagehide", endConversation);

boot();
