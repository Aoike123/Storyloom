/** Reveal received text only; append updates never restart an existing paragraph. */
export class TextReveal {
  target: string;
  shown: string;
  key: string;
  private active: boolean;
  private last = 0;
  private credit = 0;
  private finishBy = 0;

  constructor(text: string, key: string, active: boolean) {
    this.target = text; this.shown = active ? '' : text; this.key = key; this.active = active;
  }

  update(text: string, key: string, active: boolean, now: number, instant = false) {
    if (key !== this.key) {
      this.key = key; this.shown = active && !instant ? '' : text;
      this.credit = 0; this.last = now; this.finishBy = 0;
    } else if (!text.startsWith(this.shown)) {
      const before = Array.from(this.shown), after = Array.from(text);
      let common = 0;
      while (common < before.length && before[common] === after[common]) common++;
      this.shown = after.slice(0, common).join('');
    }
    if (!active && this.active) this.finishBy = now + 320;
    if (active) this.finishBy = 0;
    if (this.shown === this.target) this.last = now;
    this.target = text;
    if (instant || (!active && !this.active && !this.finishBy)) this.finish();
    if (this.shown === this.target) this.finishBy = 0;
    this.active = active;
    if (!this.last) this.last = now;
  }

  finish() {this.shown = this.target; this.credit = 0; this.finishBy = 0;}

  advance(now: number) {
    if (this.shown === this.target) return false;
    if (this.finishBy && now >= this.finishBy) {this.finish(); return false;}
    const pending = Array.from(this.target.slice(this.shown.length));
    const dt = Math.max(0, Math.min(64, now - this.last));
    this.last = now;
    // Normal text reads smoothly; large chunks catch up instead of forming a long queue.
    const rate = Math.max(42, pending.length / (this.finishBy ? Math.max(.016, (this.finishBy - now) / 1000) : .22));
    this.credit += dt * rate / 1000;
    const count = Math.min(pending.length, Math.floor(this.credit));
    if (count) {this.shown += pending.slice(0, count).join(''); this.credit -= count;}
    if (this.shown === this.target) {this.credit = 0; this.finishBy = 0; return false;}
    return true;
  }
}
