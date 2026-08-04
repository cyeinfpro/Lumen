import type { ChatStateGetter } from "./types";
import {
  isConversationMutationCurrent,
  markSendRequestSubmitted,
  trackSendRequest,
  _conversationMutationFence,
  _userSessionFence,
} from "./runtime";

export type GenerationRequestFence = {
  convId: string;
  userId: string | null;
  conversationFence: number;
  userFence: number;
  controller: AbortController;
  release: () => void;
};

export function createGenerationRequestFence(
  convId: string,
  userId: string | null,
): GenerationRequestFence {
  const controller = new AbortController();
  return {
    convId,
    userId,
    conversationFence: _conversationMutationFence.snapshot(),
    userFence: _userSessionFence.snapshot(),
    controller,
    release: trackSendRequest(controller),
  };
}

export function generationRequestIsCurrent(
  get: ChatStateGetter,
  request: GenerationRequestFence,
): boolean {
  const state = get();
  return (
    !request.controller.signal.aborted &&
    state.currentUserId === request.userId &&
    isConversationMutationCurrent(
      state.currentConvId,
      request.convId,
      request.conversationFence,
    ) &&
    _userSessionFence.isCurrent(request.userFence)
  );
}

export function markGenerationRequestSubmitted(
  request: GenerationRequestFence,
): void {
  markSendRequestSubmitted(request.controller);
}
