/** The CryptTube logo ("Epitaph"), drawn inline so it takes the theme's colors: the mark
 *  and TUBE in the accent, CRYPT in the text color. Outlined paths, no font needed. */

const CRYPT = "M60 -113V-587L173 -700H501L612 -589V-497H496V-548L443 -600H233L176 -544V-156L233 -100H443L496 -152V-203H612V-111L501 0H173ZM1309.0 -226V0H1193.0V-198L1121.0 -277H893.0V0H777.0V-700H1203.0L1305.0 -597V-382L1232.0 -309ZM893.0 -373H1151.0L1191.0 -413V-562L1151.0 -602H893.0ZM1686.0 -288 1439.0 -700H1566.0L1743.0 -396H1745.0L1922.0 -700H2049.0L1802.0 -288V0H1686.0ZM2184.0 -700H2610.0L2712.0 -597V-359L2609.0 -255H2300.0V0H2184.0ZM2558.0 -353 2598.0 -393V-562L2558.0 -602H2300.0V-353ZM3042.0 -601H2832.0V-700H3368.0V-601H3158.0V0H3042.0Z";
const TUBE = "M3678.0 -601H3468.0V-700H4004.0V-601H3794.0V0H3678.0ZM4154.0 -117V-700H4270.0V-156L4327.0 -100H4533.0L4590.0 -156V-700H4706.0V-117L4589.0 0H4271.0ZM4906.0 -700H5330.0L5427.0 -603V-407L5384.0 -364L5458.0 -290V-105L5353.0 0H4906.0ZM5272.0 -399 5313.0 -440V-562L5273.0 -602H5019.0V-399ZM5296.0 -98 5344.0 -146V-258L5296.0 -305H5019.0V-98ZM5628.0 -700H6116.0V-601H5744.0V-401H6087.0V-303H5744.0V-99H6116.0V0H5628.0Z";

/** The tombstone mark in the current text color, on a 256×256 grid. */
function Mark() {
  return (
    <>
      <path d="M65 228 L65 112 A63 63 0 0 1 191 112 L191 228" fill="none" stroke="currentColor" strokeWidth="18" strokeLinejoin="round" />
      <path d="M104 92 L104 160 L162 126 Z" fill="currentColor" stroke="currentColor" strokeWidth="8" strokeLinejoin="round" />
      <rect x="96" y="178" width="64" height="8" rx="4" fill="currentColor" opacity=".55" />
      <rect x="108" y="196" width="40" height="8" rx="4" fill="currentColor" opacity=".55" />
      <rect x="28" y="228" width="200" height="12" rx="6" fill="currentColor" />
    </>
  );
}

function Wordmark({ x, y }: { x: number; y: number }) {
  return (
    <g transform={`translate(${x} ${y}) scale(0.12571)`}>
      <path d={CRYPT} className="fill-text" />
      <path d={TUBE} className="fill-accent" />
    </g>
  );
}

type Props = { className?: string };

/** The mark alone, for small spaces (below 32px tall). */
export function LogoMark({ className }: Props) {
  return (
    <svg viewBox="0 0 256 256" className={`text-accent ${className ?? ""}`} role="img" aria-label="CryptTube">
      <Mark />
    </svg>
  );
}

/** Mark and wordmark side by side (app header; at least 24px tall). */
export function LogoHorizontal({ className }: Props) {
  return (
    <svg viewBox="0 24 1057 232" className={className} role="img" aria-label="CryptTube">
      <g className="text-accent"><Mark /></g>
      <Wordmark x={260.46} y={184} />
    </svg>
  );
}

/** Mark above the wordmark (sign-in and setup screens). */
export function LogoStacked({ className }: Props) {
  return (
    <svg viewBox="0 24 817 368" className={className} role="img" aria-label="CryptTube">
      <g className="text-accent" transform="translate(280.66 0)"><Mark /></g>
      <Wordmark x={20.46} y={364} />
    </svg>
  );
}
