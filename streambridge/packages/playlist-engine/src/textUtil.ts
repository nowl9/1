/**
 * Lightweight text similarity helpers used by TrackMatcher. We avoid pulling
 * in heavyweight NLP libs so the engine runs cleanly on both Node and React
 * Native bundlers.
 */

const PARENS_RE = /\s*[\(\[][^\)\]]*[\)\]]\s*/g;
const FEAT_RE = /\b(feat\.?|featuring|with|prod\.? by)\b.*$/i;
const REMASTER_RE = /-?\s*(remaster(ed)?|deluxe|expanded|edit|version|mix)\b.*$/i;
const NON_ALNUM = /[^\p{Letter}\p{Number}\s]/gu;

export function normalizeTitle(s: string): string {
  return s
    .toLowerCase()
    .replace(PARENS_RE, ' ')
    .replace(FEAT_RE, ' ')
    .replace(REMASTER_RE, ' ')
    .replace(NON_ALNUM, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

export function normalizeArtist(a: string): string {
  return a
    .toLowerCase()
    .replace(/\b(the|feat\.?|featuring|&|and|x)\b/g, ' ')
    .replace(NON_ALNUM, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

export function levenshtein(a: string, b: string): number {
  if (a === b) return 0;
  if (!a.length) return b.length;
  if (!b.length) return a.length;
  const prev = new Array<number>(b.length + 1);
  const curr = new Array<number>(b.length + 1);
  for (let j = 0; j <= b.length; j++) prev[j] = j;
  for (let i = 1; i <= a.length; i++) {
    curr[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      curr[j] = Math.min(curr[j - 1]! + 1, prev[j]! + 1, prev[j - 1]! + cost);
    }
    for (let j = 0; j <= b.length; j++) prev[j] = curr[j]!;
  }
  return prev[b.length]!;
}

export function similarity(a: string, b: string): number {
  if (!a && !b) return 1;
  if (!a || !b) return 0;
  const dist = levenshtein(a, b);
  return 1 - dist / Math.max(a.length, b.length);
}

export function jaccard(a: string[], b: string[]): number {
  if (!a.length && !b.length) return 1;
  const A = new Set(a);
  const B = new Set(b);
  let inter = 0;
  for (const x of A) if (B.has(x)) inter++;
  const union = A.size + B.size - inter;
  return union === 0 ? 0 : inter / union;
}
