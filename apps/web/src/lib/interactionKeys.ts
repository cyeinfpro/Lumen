/** Do not treat IME confirmation or key auto-repeat as a new UI action. */
export function isImeOrRepeatedKey(event: {
  isComposing?: boolean;
  keyCode?: number;
  repeat?: boolean;
}): boolean {
  // Safari can end composition before dispatching the confirming keydown.
  return event.isComposing === true || event.keyCode === 229 || event.repeat === true;
}
