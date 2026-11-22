import type { Metadata } from "next";
import { VoiceWorkspace } from "./voice-workspace";

export const metadata: Metadata = { title: "声音 · Lumen" };

export default function VoicePage() {
  return <VoiceWorkspace />;
}
