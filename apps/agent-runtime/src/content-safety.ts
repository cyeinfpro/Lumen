const MINOR = /\b(?:child|children|minor|minors|underage|prepubescent|preteen|toddler|infant|schoolgirl|schoolboy)\b|未成年(?:人)?|儿童|幼童|幼女|幼男|小孩/iu;
const SEXUAL = /\b(?:porn(?:ography|ographic)?|sex(?:ual|ually)?|rape|nude|naked|genitals?|molest(?:ation|ed)?|explicit)\b|色情|性爱|性交|裸照|裸体|性器官|强奸|性侵|性虐待/iu;
const ACTION = /\b(?:create|generate|make|produce|write|depict|draw|render|show|send|share|distribute|distribution|upload|download|sell|find|get|give me|provide)\b|生成|制作|创作|描写|绘制|展示|发送|分享|传播|上传|下载|售卖|寻找|提供/iu;
const ILLICIT_TERM = /\b(?:c\s*s\s*a\s*m|csem|child\s+(?:sex(?:ual)?\s+abuse\s+material|porn(?:ography)?))\b|儿童色情|未成年人色情/iu;
const PROTECTIVE_CONTEXT = /\b(?:prevent|prevention|report|reporting|detect|detection|protect|protection|investigat(?:e|ion)|prosecut(?:e|ion)|law|legal|policy|awareness|victim support|against)\b|预防|举报|检测|保护|调查|起诉|法律|政策|科普|受害者援助|反对|抵制/iu;
const REFUSAL_CONTEXT = /\b(?:cannot|can't|will not|won't|decline to|refuse to|do not|don't)\b|不能|无法|不会|拒绝/iu;
const NEGATED_REFUSAL = /\b(?:do not|don't|never)\s+(?:refuse|decline)\b|不要拒绝|不得拒绝/iu;
const CLAUSE_SPLIT = /(?:[.!?;:,\n。！？；：，]+|\b(?:but|however|then)\b)/iu;
const RAW_MINOR_OR_ILLICIT = /(?:child|minor|underage|preteen|toddler|infant|schoolgirl|schoolboy|c[\W_]*s[\W_]*[ae][\W_]*m|未成年|儿童|幼童|幼女|幼男|小孩|儿童色情)/iu;

export const CONTENT_SAFETY_HOLDBACK_CHARS = 768;

function normalizeSafetyText(value: string): string {
  return value
    .normalize("NFKC")
    .toLocaleLowerCase("en-US")
    .replace(/[\p{P}\p{S}\p{Z}\p{C}]+/gu, " ")
    .replace(/\s+/gu, " ")
    .trim();
}

function safetyClauses(value: string): string[] {
  const normalized = value
    .normalize("NFKC")
    .toLocaleLowerCase("en-US")
    .replace(/\bc[\W_]*s[\W_]*a[\W_]*m\b/giu, "csam")
    .replace(/\bc[\W_]*s[\W_]*e[\W_]*m\b/giu, "csem");
  return normalized
    .split(CLAUSE_SPLIT)
    .map(normalizeSafetyText)
    .filter((clause) => clause.length > 0);
}

function protectiveOrRefusal(value: string): boolean {
  if (PROTECTIVE_CONTEXT.test(value)) return true;
  return REFUSAL_CONTEXT.test(value) && !NEGATED_REFUSAL.test(value);
}

function unsafeClause(value: string): boolean {
  const direct = ILLICIT_TERM.test(value);
  const conjunctive = MINOR.test(value) && SEXUAL.test(value);
  return (
    (direct || conjunctive) &&
    ACTION.test(value) &&
    !protectiveOrRefusal(value)
  );
}

export function containsHighConfidenceCse(value: string): boolean {
  if (!RAW_MINOR_OR_ILLICIT.test(value)) return false;
  for (const clause of safetyClauses(value)) {
    for (let offset = 0; offset < clause.length; offset += 320) {
      if (unsafeClause(clause.slice(offset, offset + 640))) return true;
    }
  }
  return false;
}
