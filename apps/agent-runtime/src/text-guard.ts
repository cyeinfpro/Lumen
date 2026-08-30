import {
  CONTENT_SAFETY_HOLDBACK_CHARS,
  containsHighConfidenceCse,
} from "./content-safety.js";

const TOOL_FRAME_OPEN = "<tool_call>";
const TOOL_FRAME_CLOSE = "</tool_call>";
const FUNCTION_OPEN = "<function=";
const MAX_PROTOCOL_FRAME_CHARS = 32_768;
const RESERVED_FUNCTIONS = new Set([
  "lumen_create_image",
  "exec_command",
  "apply_patch",
  "bash",
  "read",
  "write",
  "edit",
  "grep",
  "find",
  "ls",
  "store_put",
  "store_get",
]);

export type TextGuardViolation =
  | "agent_provider_protocol_error"
  | "content_policy_violation";

export interface TextGuardResult {
  readonly delta: string;
  readonly violation: TextGuardViolation | null;
  readonly replacementText: string | null;
}

interface ProtocolAnalysis {
  readonly confirmedIndex: number | null;
  readonly potentialIndex: number | null;
}

function backtickRun(value: string, index: number): number {
  let end = index;
  while (value[end] === "`") end += 1;
  return end - index;
}

function protocolInLine(
  line: string,
  absoluteOffset: number,
): ProtocolAnalysis {
  let inlineTicks = 0;
  for (let index = 0; index < line.length;) {
    if (line[index] === "`") {
      const run = backtickRun(line, index);
      if (inlineTicks === 0) inlineTicks = run;
      else if (inlineTicks === run) inlineTicks = 0;
      index += run;
      continue;
    }
    if (inlineTicks !== 0) {
      index += 1;
      continue;
    }
    if (line[index] !== "<") {
      index += 1;
      continue;
    }
    const remainder = line.slice(index);
    if (TOOL_FRAME_OPEN.startsWith(remainder)) {
      return { confirmedIndex: null, potentialIndex: absoluteOffset + index };
    }
    if (remainder.startsWith(TOOL_FRAME_OPEN)) {
      const close = line.indexOf(TOOL_FRAME_CLOSE, index + TOOL_FRAME_OPEN.length);
      if (close < 0) {
        if (remainder.length > MAX_PROTOCOL_FRAME_CHARS) {
          return { confirmedIndex: absoluteOffset + index, potentialIndex: null };
        }
        return { confirmedIndex: null, potentialIndex: absoluteOffset + index };
      }
      return { confirmedIndex: absoluteOffset + index, potentialIndex: null };
    }
    if (FUNCTION_OPEN.startsWith(remainder)) {
      return { confirmedIndex: null, potentialIndex: absoluteOffset + index };
    }
    if (remainder.startsWith(FUNCTION_OPEN)) {
      const close = line.indexOf(">", index + FUNCTION_OPEN.length);
      if (close < 0) {
        return { confirmedIndex: null, potentialIndex: absoluteOffset + index };
      }
      const name = line.slice(index + FUNCTION_OPEN.length, close).trim();
      if (RESERVED_FUNCTIONS.has(name)) {
        return { confirmedIndex: absoluteOffset + index, potentialIndex: null };
      }
      index = close + 1;
      continue;
    }
    index += 1;
  }
  return { confirmedIndex: null, potentialIndex: null };
}

export function analyzeReservedProtocol(value: string): ProtocolAnalysis {
  let offset = 0;
  let fenceCharacter: "`" | "~" | null = null;
  let fenceLength = 0;
  while (offset < value.length) {
    const newline = value.indexOf("\n", offset);
    const end = newline < 0 ? value.length : newline + 1;
    const line = value.slice(offset, end);
    const withoutNewline = line.endsWith("\n") ? line.slice(0, -1) : line;
    const leading = withoutNewline.match(/^ {0,3}/u)?.[0].length ?? 0;
    const body = withoutNewline.slice(leading);
    const fenceMatch = body.match(/^(`{3,}|~{3,})/u);
    if (fenceCharacter !== null) {
      if (
        fenceMatch &&
        fenceMatch[1]?.[0] === fenceCharacter &&
        fenceMatch[1].length >= fenceLength
      ) {
        fenceCharacter = null;
        fenceLength = 0;
      }
      offset = end;
      continue;
    }
    if (fenceMatch) {
      fenceCharacter = fenceMatch[1]?.[0] as "`" | "~";
      fenceLength = fenceMatch[1]?.length ?? 3;
      offset = end;
      continue;
    }
    if (/^(?:>| {4}|\t)/u.test(withoutNewline)) {
      offset = end;
      continue;
    }
    const analysis = protocolInLine(line, offset);
    if (analysis.confirmedIndex !== null || analysis.potentialIndex !== null) {
      return analysis;
    }
    offset = end;
  }
  return { confirmedIndex: null, potentialIndex: null };
}

interface ScannerResult {
  readonly safe: string;
  readonly violation: boolean;
}

class IncrementalProtocolScanner {
  private safe = "";
  private candidate = "";
  private blocked = false;
  private lineStart = true;
  private leadingSpaces = 0;
  private lineLiteral = false;
  private openingFenceCharacter: "`" | "~" | null = null;
  private openingFenceCount = 0;
  private inlineTicks = 0;
  private tickRun = 0;
  private fenceCharacter: "`" | "~" | null = null;
  private fenceLength = 0;
  private fenceOpeningRun = false;
  private fenceClosePossible = false;
  private fenceCloseLeadingSpaces = 0;
  private fenceCloseMarkers = 0;

  get retainedChars(): number {
    return this.candidate.length + this.openingFenceCount + this.tickRun;
  }

  push(value: string): ScannerResult {
    this.safe = "";
    for (const character of value) {
      this.consume(character);
      if (this.blocked) break;
    }
    return { safe: this.safe, violation: this.blocked };
  }

  finish(): ScannerResult {
    this.safe = "";
    this.finalizeTicks();
    if (this.candidate) {
      if (this.candidateCouldBeReserved()) this.blocked = true;
      else this.releaseCandidate();
    }
    return { safe: this.safe, violation: this.blocked };
  }

  private append(value: string): void {
    this.safe += value;
  }

  private consume(character: string): void {
    if (this.fenceCharacter !== null) {
      this.consumeFence(character);
      return;
    }
    if (this.lineLiteral) {
      this.append(character);
      if (character === "\n") this.resetLine();
      return;
    }
    if (this.lineStart) {
      this.consumeLineStart(character);
      return;
    }
    this.consumeNormal(character);
  }

  private consumeLineStart(character: string): void {
    if (character === "\n") {
      this.append(character);
      this.resetLine();
      return;
    }
    if (this.openingFenceCharacter !== null) {
      if (character === this.openingFenceCharacter) {
        this.openingFenceCount += 1;
        this.append(character);
        if (this.openingFenceCount >= 3) {
          this.fenceCharacter = this.openingFenceCharacter;
          this.fenceLength = this.openingFenceCount;
          this.fenceOpeningRun = true;
          this.openingFenceCharacter = null;
          this.openingFenceCount = 0;
          this.lineStart = false;
        }
        return;
      }
      if (this.openingFenceCharacter === "`") {
        this.inlineTicks = this.openingFenceCount;
      }
      this.openingFenceCharacter = null;
      this.openingFenceCount = 0;
      this.lineStart = false;
      this.consumeNormal(character);
      return;
    }
    if (character === " " && this.leadingSpaces < 4) {
      this.leadingSpaces += 1;
      this.append(character);
      if (this.leadingSpaces === 4) {
        this.lineLiteral = true;
        this.lineStart = false;
      }
      return;
    }
    if (character === "\t" || character === ">") {
      this.append(character);
      this.lineLiteral = true;
      this.lineStart = false;
      return;
    }
    if (
      this.leadingSpaces <= 3 &&
      (character === "`" || character === "~")
    ) {
      this.openingFenceCharacter = character;
      this.openingFenceCount = 1;
      this.append(character);
      return;
    }
    this.lineStart = false;
    this.consumeNormal(character);
  }

  private consumeFence(character: string): void {
    this.append(character);
    if (this.fenceOpeningRun) {
      if (character === this.fenceCharacter) {
        this.fenceLength += 1;
        return;
      }
      this.fenceOpeningRun = false;
      if (character === "\n") this.startFenceLine();
      return;
    }
    if (character === "\n") {
      if (
        this.fenceClosePossible &&
        this.fenceCloseMarkers >= this.fenceLength
      ) {
        this.fenceCharacter = null;
        this.fenceLength = 0;
        this.resetLine();
      } else {
        this.startFenceLine();
      }
      return;
    }
    if (!this.fenceClosePossible) return;
    if (this.fenceCloseMarkers === 0 && character === " ") {
      this.fenceCloseLeadingSpaces += 1;
      if (this.fenceCloseLeadingSpaces > 3) this.fenceClosePossible = false;
      return;
    }
    if (character === this.fenceCharacter) {
      this.fenceCloseMarkers += 1;
      return;
    }
    if (this.fenceCloseMarkers > 0 && (character === " " || character === "\t")) {
      return;
    }
    this.fenceClosePossible = false;
  }

  private startFenceLine(): void {
    this.fenceClosePossible = true;
    this.fenceCloseLeadingSpaces = 0;
    this.fenceCloseMarkers = 0;
  }

  private consumeNormal(character: string): void {
    if (this.candidate) {
      this.consumeCandidate(character);
      return;
    }
    if (character === "`") {
      this.tickRun += 1;
      this.append(character);
      return;
    }
    this.finalizeTicks();
    if (character === "\n") {
      this.append(character);
      this.inlineTicks = 0;
      this.resetLine();
      return;
    }
    if (this.inlineTicks === 0 && character === "<") {
      this.candidate = character;
      return;
    }
    this.append(character);
  }

  private finalizeTicks(): void {
    if (this.tickRun === 0) return;
    if (this.inlineTicks === 0) this.inlineTicks = this.tickRun;
    else if (this.inlineTicks === this.tickRun) this.inlineTicks = 0;
    this.tickRun = 0;
  }

  private consumeCandidate(character: string): void {
    this.candidate += character;
    if (this.candidate === TOOL_FRAME_OPEN) {
      this.blocked = true;
      return;
    }
    if (
      TOOL_FRAME_OPEN.startsWith(this.candidate) ||
      FUNCTION_OPEN.startsWith(this.candidate)
    ) {
      return;
    }
    if (this.candidate.startsWith(FUNCTION_OPEN)) {
      const functionTail = this.candidate.slice(FUNCTION_OPEN.length);
      const close = functionTail.indexOf(">");
      if (close >= 0) {
        const name = functionTail.slice(0, close).trim();
        if (RESERVED_FUNCTIONS.has(name)) {
          this.blocked = true;
          return;
        }
        this.releaseCandidate();
        return;
      }
      const partialName = functionTail.trim();
      const couldBeReserved =
        partialName.length === 0 ||
        Array.from(RESERVED_FUNCTIONS).some((name) =>
          name.startsWith(partialName)
        );
      if (couldBeReserved && this.candidate.length <= 128) return;
    }
    this.releaseCandidate();
  }

  private candidateCouldBeReserved(): boolean {
    if (this.candidate.length < 3) return false;
    if (
      TOOL_FRAME_OPEN.startsWith(this.candidate) ||
      FUNCTION_OPEN.startsWith(this.candidate)
    ) {
      return true;
    }
    if (!this.candidate.startsWith(FUNCTION_OPEN)) return false;
    const partialName = this.candidate.slice(FUNCTION_OPEN.length).trim();
    return (
      partialName.length > 0 &&
      Array.from(RESERVED_FUNCTIONS).some((name) =>
        name.startsWith(partialName)
      )
    );
  }

  private releaseCandidate(): void {
    this.append(this.candidate);
    this.candidate = "";
  }

  private resetLine(): void {
    this.lineStart = true;
    this.leadingSpaces = 0;
    this.lineLiteral = false;
    this.openingFenceCharacter = null;
    this.openingFenceCount = 0;
    this.tickRun = 0;
  }
}

function flushSafetyHoldback(value: string): [string, string] {
  if (value.length <= CONTENT_SAFETY_HOLDBACK_CHARS) return ["", value];
  let emitEnd = value.length - CONTENT_SAFETY_HOLDBACK_CHARS;
  const previous = value.charCodeAt(emitEnd - 1);
  const next = value.charCodeAt(emitEnd);
  if (
    previous >= 0xd800 && previous <= 0xdbff &&
    next >= 0xdc00 && next <= 0xdfff
  ) {
    emitEnd -= 1;
  }
  return [value.slice(0, emitEnd), value.slice(emitEnd)];
}

export class StreamingTextGuard {
  private scanner = new IncrementalProtocolScanner();
  private pending = "";
  private blocked = false;

  get text(): string {
    return this.pending;
  }

  get retainedChars(): number {
    return Array.from(this.pending).length + this.scanner.retainedChars;
  }

  replace(initialText: string): void {
    void initialText;
    this.scanner = new IncrementalProtocolScanner();
    this.pending = "";
    this.blocked = false;
  }

  push(delta: string): TextGuardResult {
    if (this.blocked || delta.length === 0) {
      return { delta: "", violation: null, replacementText: null };
    }
    const scanned = this.scanner.push(delta);
    this.pending += scanned.safe;
    if (scanned.violation) {
      this.blocked = true;
      return {
        delta: "",
        violation: "agent_provider_protocol_error",
        replacementText: this.pending,
      };
    }
    if (containsHighConfidenceCse(this.pending)) {
      this.pending = "";
      this.blocked = true;
      return {
        delta: "",
        violation: "content_policy_violation",
        replacementText: "",
      };
    }
    const [output, retained] = flushSafetyHoldback(this.pending);
    this.pending = retained;
    return { delta: output, violation: null, replacementText: null };
  }

  finish(): TextGuardResult {
    if (this.blocked) {
      return { delta: "", violation: null, replacementText: null };
    }
    const scanned = this.scanner.finish();
    this.pending += scanned.safe;
    if (scanned.violation) {
      this.blocked = true;
      return {
        delta: "",
        violation: "agent_provider_protocol_error",
        replacementText: this.pending,
      };
    }
    if (containsHighConfidenceCse(this.pending)) {
      this.pending = "";
      this.blocked = true;
      return {
        delta: "",
        violation: "content_policy_violation",
        replacementText: "",
      };
    }
    const output = this.pending;
    this.pending = "";
    return { delta: output, violation: null, replacementText: null };
  }
}

export function completeTextGuardViolation(
  value: string,
): TextGuardViolation | null {
  const guard = new StreamingTextGuard();
  const streamed = guard.push(value);
  if (streamed.violation !== null) return streamed.violation;
  return guard.finish().violation;
}
