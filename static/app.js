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
  lesson: null,
  exerciseIdx: 0,
  correctCount: 0,
  // Vocab
  vocabSession: null,
  vocabStudyIdx: 0,
  vocabExIdx: 0,
  vocabCorrectCount: 0,
};

const els = {
  picker: document.getElementById("picker"),
  conversation: document.getElementById("conversation"),
  modeCards: document.getElementById("modeCards"),
  convLabel: document.getElementById("convLabel"),
  convStatus: document.getElementById("convStatus"),
  orb: document.getElementById("orb"),
  transcript: document.getElementById("transcript"),
  backBtn: document.getElementById("backBtn"),
  endBtn: document.getElementById("endBtn"),
  muteBtn: document.getElementById("muteBtn"),
  app: document.getElementById("app"),
  // Sub-picker
  subpicker: document.getElementById("subpicker"),
  subLabel: document.getElementById("subLabel"),
  subSubtitle: document.getElementById("subSubtitle"),
  subBackBtn: document.getElementById("subBackBtn"),
  actSpeak: document.getElementById("actSpeak"),
  actGrammar: document.getElementById("actGrammar"),
  // Grammar
  grammar: document.getElementById("grammar"),
  grammarBody: document.getElementById("grammarBody"),
  gramLabel: document.getElementById("gramLabel"),
  gramStatus: document.getElementById("gramStatus"),
  gramBackBtn: document.getElementById("gramBackBtn"),
  // Vocab
  actVocab: document.getElementById("actVocab"),
  vocab: document.getElementById("vocab"),
  vocabBody: document.getElementById("vocabBody"),
  vocabLabel: document.getElementById("vocabLabel"),
  vocabStatus: document.getElementById("vocabStatus"),
  vocabBackBtn: document.getElementById("vocabBackBtn"),
};

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
      <div class="initials">${initials(m.label)}</div>
      <div class="info">
        <div class="name">${m.label}</div>
        <div class="sub">${m.subtitle}</div>
      </div>
      <div class="arrow">→</div>
    `;
    card.addEventListener("click", () => openSubpicker(m));
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

// ---------- Sub-picker (Hablar / Gramática) ----------

function openSubpicker(mode) {
  state.mode = mode;
  els.subLabel.textContent = mode.label;
  els.app.style.setProperty("--mc-color", mode.color);
  document.documentElement.style.setProperty("--mc-color", mode.color);
  showScreen("subpicker");
}

els.subBackBtn.addEventListener("click", () => showScreen("picker"));

let activityClickGuard = false;
function guardClick(fn) {
  if (activityClickGuard) return;
  activityClickGuard = true;
  setTimeout(() => { activityClickGuard = false; }, 1500);
  fn();
}
els.actSpeak.addEventListener("click", () => guardClick(() => {
  if (state.mode) startVoice(state.mode);
}));
els.actGrammar.addEventListener("click", () => guardClick(() => {
  if (state.mode) startGrammar(state.mode);
}));
els.actVocab.addEventListener("click", () => guardClick(() => {
  if (state.mode) startVocab(state.mode);
}));

// ---------- Conversation lifecycle ----------

async function startVoice(mode) {
  state.mode = mode;
  els.convLabel.textContent = mode.label;
  els.convStatus.textContent = "Conectando…";
  els.transcript.innerHTML = "";
  els.app.style.setProperty("--mc-color", mode.color);
  document.documentElement.style.setProperty("--mc-color", mode.color);
  showScreen("conversation");
  setOrb("idle");

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
  showScreen(state.mode ? "subpicker" : "picker");
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

// ---------- Grammar ----------

els.gramBackBtn.addEventListener("click", () => showScreen("subpicker"));

async function startGrammar(mode) {
  els.gramLabel.textContent = mode.label;
  els.gramStatus.textContent = "Cargando…";
  els.grammarBody.innerHTML = '<div class="grammar-loading">Generando tu lección de hoy…</div>';
  showScreen("grammar");

  state.lesson = null;
  state.exerciseIdx = 0;
  state.correctCount = 0;

  try {
    const r = await fetch(`/api/grammar/today?mode=${encodeURIComponent(mode.id)}`);
    if (!r.ok) {
      const txt = await r.text();
      throw new Error(`HTTP ${r.status}: ${txt.slice(0, 200)}`);
    }
    const lesson = await r.json();
    state.lesson = lesson;
    els.gramStatus.textContent = lesson.level || "";
    renderLessonIntro();
  } catch (err) {
    console.error("[grammar] load failed", err);
    els.gramStatus.textContent = "Error";
    els.grammarBody.innerHTML = `
      <div class="grammar-loading">
        No se pudo cargar la lección.<br><br>
        <button class="gram-cta secondary" id="retryBtn">Reintentar</button>
      </div>`;
    document.getElementById("retryBtn").addEventListener("click", () => startGrammar(mode));
  }
}

function renderLessonIntro() {
  const l = state.lesson;
  els.grammarBody.innerHTML = `
    <div class="gram-meta">${l.level} · ${l.exercises.length} ejercicios</div>
    <h3 class="gram-title">${escapeHtml(l.title)}</h3>
    <div class="gram-explanation">${escapeHtml(l.explanation)}</div>
    <div class="gram-section-title">Examples</div>
    <div class="gram-examples">
      ${l.examples.map(ex => `
        <div class="gram-example">
          <div class="en">${escapeHtml(ex.en)}</div>
          <div class="es">${escapeHtml(ex.translation || "")}</div>
        </div>
      `).join("")}
    </div>
    <button class="gram-cta" id="startExBtn">Empezar ejercicios →</button>
  `;
  els.grammarBody.scrollTop = 0;
  document.getElementById("startExBtn").addEventListener("click", () => {
    state.exerciseIdx = 0;
    state.correctCount = 0;
    renderExercise();
  });
}

function renderExercise() {
  const l = state.lesson;
  const idx = state.exerciseIdx;
  if (idx >= l.exercises.length) {
    renderResult();
    return;
  }
  const ex = l.exercises[idx];
  const total = l.exercises.length;

  if (ex.type === "mc") {
    els.grammarBody.innerHTML = `
      <div class="gram-progress">Pregunta ${idx + 1} de ${total}</div>
      <div class="gram-question">
        <div class="q">${escapeHtml(ex.question)}</div>
        <div class="gram-options" id="opts">
          ${ex.options.map((opt, i) => `
            <button class="gram-option" data-i="${i}">${escapeHtml(opt)}</button>
          `).join("")}
        </div>
        <div id="feedback"></div>
      </div>
    `;
    const opts = els.grammarBody.querySelectorAll(".gram-option");
    opts.forEach(btn => {
      btn.addEventListener("click", () => handleMcAnswer(btn, ex, opts));
    });
  } else {
    // fill
    els.grammarBody.innerHTML = `
      <div class="gram-progress">Pregunta ${idx + 1} de ${total}</div>
      <div class="gram-question">
        <div class="q">${escapeHtml(ex.question)}</div>
        <input class="gram-fill-input" id="fillInput" type="text" autocomplete="off" autocapitalize="none" placeholder="Escribe tu respuesta…">
        <div id="feedback"></div>
        <button class="gram-cta" id="checkBtn" style="margin-top: 0.8rem;">Comprobar</button>
      </div>
    `;
    const input = document.getElementById("fillInput");
    const checkBtn = document.getElementById("checkBtn");
    input.focus();
    const submit = () => handleFillAnswer(input, ex, checkBtn);
    checkBtn.addEventListener("click", submit);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); submit(); }
    });
  }
  els.grammarBody.scrollTop = 0;
}

function handleMcAnswer(btn, ex, allBtns) {
  const chosen = btn.textContent;
  const correct = ex.correct;
  const isCorrect = chosen === correct;
  allBtns.forEach(b => { b.disabled = true; });
  if (isCorrect) {
    btn.classList.add("correct");
  } else {
    btn.classList.add("wrong");
    allBtns.forEach(b => {
      if (b.textContent === correct) b.classList.add("correct");
    });
  }
  if (isCorrect) state.correctCount++;
  showFeedbackAndNext(isCorrect, ex, chosen);
}

function handleFillAnswer(input, ex, checkBtn) {
  const raw = (input.value || "").trim().toLowerCase().replace(/[.,!?;:]+$/, "");
  if (!raw) return;
  const accepted = [ex.correct.toLowerCase(), ...((ex.accept || []).map(a => a.toLowerCase()))];
  const isCorrect = accepted.includes(raw);
  input.disabled = true;
  checkBtn.disabled = true;
  input.classList.add(isCorrect ? "correct" : "wrong");
  if (isCorrect) state.correctCount++;
  showFeedbackAndNext(isCorrect, ex, raw);
}

function showFeedbackAndNext(isCorrect, ex, userAnswer) {
  const fb = document.getElementById("feedback");
  fb.className = `gram-feedback ${isCorrect ? "correct" : "wrong"}`;
  const icon = isCorrect ? "✓" : "✗";
  const head = isCorrect ? "Correct" : "Not quite";
  const answerLine = isCorrect ? "" : `<span class="answer">Correct answer: <strong>${escapeHtml(ex.correct)}</strong></span>`;
  fb.innerHTML = `<strong>${icon} ${head}.</strong> ${escapeHtml(ex.explanation || "")}${answerLine}`;

  // Save attempt (fire-and-forget). El servidor revalida is_correct internamente.
  fetch(`/api/grammar/attempt?mode=${encodeURIComponent(state.mode.id)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      lesson_id: state.lesson.lesson_id,
      exercise_index: state.exerciseIdx,
      user_answer: String(userAnswer || ""),
    }),
  }).catch(err => console.warn("[grammar] attempt save failed", err));

  // Next button
  const next = document.createElement("button");
  next.className = "gram-cta";
  next.style.marginTop = "0.8rem";
  next.textContent = state.exerciseIdx + 1 >= state.lesson.exercises.length ? "Ver resultado →" : "Siguiente →";
  next.addEventListener("click", () => {
    state.exerciseIdx++;
    renderExercise();
  });
  fb.parentElement.appendChild(next);
  next.focus();
}

function renderResult() {
  const total = state.lesson.exercises.length;
  const score = state.correctCount;
  const pct = Math.round((score / total) * 100);
  let label = "Sigue practicando, mañana otra";
  if (pct >= 80) label = "¡Genial! Lo dominas";
  else if (pct >= 60) label = "Buen trabajo";
  else if (pct >= 40) label = "Vas por buen camino";

  els.grammarBody.innerHTML = `
    <div class="gram-result">
      <div class="score">${score} / ${total}</div>
      <div class="label">${label}</div>
      <div class="actions">
        <button class="gram-cta" id="speakBtn">Hablar de esto con ${escapeHtml(state.mode.label)}</button>
        <button class="gram-cta secondary" id="repeatBtn">Repetir con ejercicios nuevos</button>
        <button class="gram-cta secondary" id="doneBtn">Terminar</button>
      </div>
    </div>
  `;
  document.getElementById("speakBtn").addEventListener("click", () => startVoice(state.mode));
  document.getElementById("repeatBtn").addEventListener("click", repeatWithNewExercises);
  document.getElementById("doneBtn").addEventListener("click", () => showScreen("subpicker"));
  els.grammarBody.scrollTop = 0;
}

async function repeatWithNewExercises() {
  if (!state.lesson || !state.mode) return;
  const lessonId = state.lesson.lesson_id;
  els.grammarBody.innerHTML = '<div class="grammar-loading">Generando ejercicios nuevos…</div>';
  els.grammarBody.scrollTop = 0;
  try {
    const r = await fetch(`/api/grammar/regenerate?mode=${encodeURIComponent(state.mode.id)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lesson_id: lessonId }),
    });
    if (!r.ok) {
      const txt = await r.text();
      throw new Error(`HTTP ${r.status}: ${txt.slice(0, 200)}`);
    }
    const lesson = await r.json();
    state.lesson = lesson;
    state.exerciseIdx = 0;
    state.correctCount = 0;
    renderExercise();
  } catch (err) {
    console.error("[grammar] regenerate failed", err);
    els.grammarBody.innerHTML = `
      <div class="grammar-loading">
        No se pudieron generar ejercicios nuevos.<br><br>
        <button class="gram-cta secondary" id="backResultBtn">Volver</button>
      </div>`;
    document.getElementById("backResultBtn").addEventListener("click", renderResult);
  }
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ---------- Vocabulary ----------

els.vocabBackBtn.addEventListener("click", () => showScreen("subpicker"));

async function startVocab(mode) {
  els.vocabLabel.textContent = mode.label;
  els.vocabStatus.textContent = "Cargando…";
  els.vocabBody.innerHTML = '<div class="grammar-loading">Preparando tus phrasal verbs…</div>';
  showScreen("vocab");

  state.vocabSession = null;
  state.vocabStudyIdx = 0;
  state.vocabExIdx = 0;
  state.vocabCorrectCount = 0;

  try {
    const r = await fetch(`/api/vocab/today?mode=${encodeURIComponent(mode.id)}`);
    if (!r.ok) {
      const txt = await r.text();
      throw new Error(`HTTP ${r.status}: ${txt.slice(0, 200)}`);
    }
    const session = await r.json();
    state.vocabSession = session;
    els.vocabStatus.textContent = session.level || "";
    renderVocabIntro();
  } catch (err) {
    console.error("[vocab] load failed", err);
    els.vocabStatus.textContent = "Error";
    els.vocabBody.innerHTML = `
      <div class="grammar-loading">
        No se pudo cargar la sesión.<br><br>
        <button class="gram-cta secondary" id="retryVocabBtn">Reintentar</button>
      </div>`;
    document.getElementById("retryVocabBtn").addEventListener("click", () => startVocab(mode));
  }
}

function renderVocabIntro() {
  const s = state.vocabSession;
  const t = s.totals || { new: 0, reviews: 0, exercises: 0 };

  if (t.new === 0 && t.reviews === 0) {
    els.vocabBody.innerHTML = `
      <div class="vocab-empty">
        <div class="big-icon">✅</div>
        <h3 class="gram-title">Hoy no toca</h3>
        <div class="gram-explanation">
          No tienes phrasal verbs nuevos pendientes ni repasos vencidos.
          Vuelve mañana para seguir avanzando.
        </div>
        <button class="gram-cta secondary" id="vocabDoneBtn" style="margin-top: 1rem;">Volver</button>
      </div>`;
    document.getElementById("vocabDoneBtn").addEventListener("click", () => showScreen("subpicker"));
    return;
  }

  // Si hay nuevos → empezamos por estudiar; si solo hay repasos → directos a ejercicios.
  els.vocabBody.innerHTML = `
    <div class="gram-meta">${s.level} · ${t.new} ${t.new === 1 ? "nuevo" : "nuevos"} · ${t.reviews} ${t.reviews === 1 ? "repaso" : "repasos"}</div>
    <h3 class="gram-title">Phrasal verbs de hoy</h3>
    <div class="gram-explanation">
      Primero veremos los phrasal verbs nuevos del día con sus ejemplos.
      Después los pondremos a prueba mezclando los nuevos con los repasos pendientes.
    </div>
    <button class="gram-cta" id="startVocabBtn">${t.new > 0 ? "Empezar a estudiar →" : "Ir a los ejercicios →"}</button>
  `;
  els.vocabBody.scrollTop = 0;
  document.getElementById("startVocabBtn").addEventListener("click", () => {
    if ((s.study || []).length > 0) {
      state.vocabStudyIdx = 0;
      renderVocabStudyCard();
    } else {
      state.vocabExIdx = 0;
      renderVocabExercise();
    }
  });
}

function renderVocabStudyCard() {
  const s = state.vocabSession;
  const idx = state.vocabStudyIdx;
  const total = s.study.length;
  const it = s.study[idx];
  const last = idx + 1 >= total;

  els.vocabBody.innerHTML = `
    <div class="gram-progress">Nuevo ${idx + 1} de ${total}</div>
    <div class="voc-card">
      <div class="voc-phrasal">${escapeHtml(it.phrasal)}</div>
      <div class="voc-meaning">${escapeHtml(it.meaning_es)}</div>
      <div class="voc-meaning-en">${escapeHtml(it.meaning_en || "")}</div>
      <div class="gram-section-title">Examples</div>
      <div class="gram-examples">
        ${(it.examples || []).map(ex => `
          <div class="gram-example">
            <div class="en">${escapeHtml(ex.en)}</div>
            <div class="es">${escapeHtml(ex.es || "")}</div>
          </div>
        `).join("")}
      </div>
    </div>
    <button class="gram-cta" id="nextStudyBtn">${last ? "Empezar ejercicios →" : "Lo entendí →"}</button>
  `;
  els.vocabBody.scrollTop = 0;
  document.getElementById("nextStudyBtn").addEventListener("click", () => {
    if (last) {
      state.vocabExIdx = 0;
      state.vocabCorrectCount = 0;
      renderVocabExercise();
    } else {
      state.vocabStudyIdx++;
      renderVocabStudyCard();
    }
  });
}

function renderVocabExercise() {
  const s = state.vocabSession;
  const idx = state.vocabExIdx;
  const total = s.exercises.length;

  if (idx >= total) {
    renderVocabResult();
    return;
  }

  const ex = s.exercises[idx];
  els.vocabBody.innerHTML = `
    <div class="gram-progress">Pregunta ${idx + 1} de ${total}</div>
    <div class="gram-question">
      <div class="q">${escapeHtml(ex.question)}</div>
      <div class="gram-options" id="opts">
        ${ex.options.map((opt, i) => `
          <button class="gram-option" data-i="${i}">${escapeHtml(opt)}</button>
        `).join("")}
      </div>
      <div id="feedback"></div>
    </div>
  `;
  const opts = els.vocabBody.querySelectorAll(".gram-option");
  opts.forEach(btn => btn.addEventListener("click", () => handleVocabAnswer(btn, ex, opts)));
  els.vocabBody.scrollTop = 0;
}

async function handleVocabAnswer(btn, ex, allBtns) {
  const chosen = btn.textContent;
  const isCorrect = chosen === ex.correct;
  allBtns.forEach(b => { b.disabled = true; });
  if (isCorrect) {
    btn.classList.add("correct");
    state.vocabCorrectCount++;
  } else {
    btn.classList.add("wrong");
    allBtns.forEach(b => {
      if (b.textContent === ex.correct) b.classList.add("correct");
    });
  }

  // Feedback con la definición y los ejemplos
  const fb = document.getElementById("feedback");
  fb.className = `gram-feedback ${isCorrect ? "correct" : "wrong"}`;
  const head = isCorrect ? "✓ Correct" : "✗ Not quite";
  const examples = (ex.examples || []).slice(0, 2).map(e => `
    <div class="gram-example" style="margin-top: 0.5rem;">
      <div class="en">${escapeHtml(e.en)}</div>
      <div class="es">${escapeHtml(e.es || "")}</div>
    </div>
  `).join("");
  fb.innerHTML = `
    <strong>${head}.</strong> <em>${escapeHtml(ex.phrasal)}</em> = ${escapeHtml(ex.correct)}.
    <span class="answer">${escapeHtml(ex.explanation || "")}</span>
    ${examples}
  `;

  // Guardar respuesta en el servidor (Leitner). Fire-and-forget.
  fetch(`/api/vocab/answer?mode=${encodeURIComponent(state.mode.id)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      phrasal_id: ex.phrasal_id,
      user_answer: chosen,
    }),
  }).catch(err => console.warn("[vocab] answer save failed", err));

  const next = document.createElement("button");
  next.className = "gram-cta";
  next.style.marginTop = "0.8rem";
  next.textContent = state.vocabExIdx + 1 >= state.vocabSession.exercises.length ? "Ver resultado →" : "Siguiente →";
  next.addEventListener("click", () => {
    state.vocabExIdx++;
    renderVocabExercise();
  });
  fb.parentElement.appendChild(next);
  next.focus();
}

function renderVocabResult() {
  const total = state.vocabSession.exercises.length;
  const score = state.vocabCorrectCount;
  const pct = total > 0 ? Math.round((score / total) * 100) : 0;
  let label = "Sigue practicando";
  if (pct >= 80) label = "¡Genial! Te los sabes";
  else if (pct >= 60) label = "Buen trabajo";
  else if (pct >= 40) label = "Vas cogiéndolos";

  els.vocabBody.innerHTML = `
    <div class="gram-result">
      <div class="score">${score} / ${total}</div>
      <div class="label">${label}</div>
      <div class="actions">
        <button class="gram-cta" id="vocSpeakBtn">Hablar de esto con ${escapeHtml(state.mode.label)}</button>
        <button class="gram-cta secondary" id="vocDoneBtn">Terminar</button>
      </div>
    </div>
  `;
  document.getElementById("vocSpeakBtn").addEventListener("click", () => startVoice(state.mode));
  document.getElementById("vocDoneBtn").addEventListener("click", () => showScreen("subpicker"));
  els.vocabBody.scrollTop = 0;
}

boot();
