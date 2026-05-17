import HexGridBG from "./HexGridBG";

export default function SiteBackground() {
  return (
    <div className="fixed inset-0 z-0 pointer-events-none overflow-hidden">
      {/* Soft ambient grape glow behind everything */}
      <div className="absolute top-1/3 left-1/2 -translate-x-1/2 w-[900px] h-[900px] bg-grape-700/5 rounded-full blur-[180px]" />

      {/* Hexagonal grid pulse */}
      <HexGridBG />

      {/* Soft top hairline */}
      <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-grape-500/10 to-transparent" />
    </div>
  );
}
