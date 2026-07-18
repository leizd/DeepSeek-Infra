import { useCallback, useEffect, useRef, useState } from "react";

import type { ChatMessage } from "../../domain/chat/types";
import {
  normalizeVoiceLanguage,
  preferredSpeechVoice,
  speechChunks,
  speechSynthesisSupported,
  speechTextFromContent,
} from "./speechText";

export interface SpeechPlayer {
  speakingMessageId: string;
  supported: boolean;
  toggleSpeak(message: ChatMessage): void;
  stop(): void;
}

export function useSpeechPlayer(onUnsupported?: () => void): SpeechPlayer {
  const [speakingMessageId, setSpeakingMessageId] = useState("");
  const queueRef = useRef<string[]>([]);
  const supported = speechSynthesisSupported();

  const stop = useCallback(() => {
    if (speechSynthesisSupported()) window.speechSynthesis.cancel();
    queueRef.current = [];
    setSpeakingMessageId("");
  }, []);

  useEffect(() => stop, [stop]);

  const speakNext = useCallback(
    (messageId: string, lang: string) => {
      const chunk = queueRef.current.shift();
      if (!chunk) {
        setSpeakingMessageId("");
        return;
      }
      const utterance = new SpeechSynthesisUtterance(chunk);
      utterance.lang = lang;
      const voice = preferredSpeechVoice(lang, window.speechSynthesis.getVoices());
      if (voice) utterance.voice = voice;
      utterance.rate = 1;
      utterance.onend = () => speakNext(messageId, lang);
      utterance.onerror = () => setSpeakingMessageId("");
      window.speechSynthesis.speak(utterance);
    },
    [],
  );

  const toggleSpeak = useCallback(
    (message: ChatMessage) => {
      if (!supported) {
        onUnsupported?.();
        return;
      }
      if (speakingMessageId === message.id) {
        stop();
        return;
      }
      const chunks = speechChunks(speechTextFromContent(message.content));
      if (!chunks.length) return;
      stop();
      queueRef.current = chunks;
      setSpeakingMessageId(message.id);
      const lang = normalizeVoiceLanguage(document.documentElement.lang || navigator.language || "zh-CN");
      speakNext(message.id, lang);
    },
    [onUnsupported, speakNext, speakingMessageId, stop, supported],
  );

  return { speakingMessageId, supported, toggleSpeak, stop };
}
