/** Preset survivor phrases: the rover speaks them, or the headset plays the returned WAV. */
export const PHRASE_LABELS = {
  help_coming: "Help is coming, stay still",
  found_you: "We found you",
  can_you_hear: "Can you hear me?",
  call_out: "Call out so we can find you",
  stay_calm: "Stay calm",
};

export async function speakPhrase(phrase, lang, { fetchImpl = fetch, play = url => new Audio(url).play() } = {}) {
  const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 10000);
  try {
    const response = await fetchImpl("/speak", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phrase, lang }), signal: abort.signal,
    });
    const reply = await response.json().catch(() => ({}));
    if (!response.ok) return `Phrase unavailable: ${String(reply.detail ?? `HTTP ${response.status}`)}`;
    if (reply.played) return `Rover speaker: "${reply.text}"`;
    // Only rover-generated TTS paths are played on the headset.
    if (typeof reply.audio_url === "string" && /^\/tts\/phrase-[a-z_]+-(en|es)\.wav$/.test(reply.audio_url)) {
      await play(reply.audio_url);
      return `No rover speaker; playing on headset: "${reply.text}"`;
    }
    return "Phrase unavailable: no audio returned";
  } catch (error) {
    return `Phrase unavailable: ${String(error)}`;
  } finally { clearTimeout(timer); }
}

/** One-line SBAR for the mission report; empty when the encounter has none. */
export function sbarLines(sbar) {
  if (!sbar || typeof sbar !== "object") return [];
  return [["S", sbar.situation], ["B", sbar.background], ["A", sbar.assessment], ["R", sbar.recommendation]]
    .filter(([, text]) => typeof text === "string" && text.trim())
    .map(([letter, text]) => `${letter}: ${text.trim()}`);
}
