import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import { ArrowRight, GitPullRequest, Shield, Zap } from "lucide-react";

const FEATURES = [
  {
    Icon: Shield,
    title: "Graph-proven findings",
    desc: "Every comment cites the real AST import chain — no hallucinated paths.",
  },
  {
    Icon: GitPullRequest,
    title: "Works on every PR",
    desc: "Installs as a GitHub App. No YAML, no CLI, no CI changes needed.",
  },
  {
    Icon: Zap,
    title: "5 free reviews",
    desc: "Start on any public repo today. No credit card required.",
  },
];

const GH_APP = "https://github.com/apps/graperoot-review/installations/new/permissions?target_id=84341876";

export default function ReviewPage() {
  return (
    <main className="min-h-screen text-white">
      <Navbar />

      {/* ── HERO ─────────────────────────────────────────────────────────── */}
      <section className="relative min-h-[100dvh] flex items-center overflow-hidden">
        <div className="relative z-10 w-full max-w-5xl mx-auto px-5 sm:px-6 pt-20 pb-10 sm:pt-24 sm:pb-12 text-center flex flex-col items-center">

          <div className="inline-flex items-center gap-2 text-[11px] font-semibold text-grape-300 bg-grape-500/10 border border-grape-500/20 px-3 py-1.5 rounded-full mb-6 tracking-widest uppercase animate-fade-in-up-delay-1">
            <span className="w-1.5 h-1.5 rounded-full bg-grape-400 animate-pulse" />
            5 free reviews — no credit card
          </div>

          <h1 className="font-sans text-[2.1rem] leading-[1.08] sm:text-5xl md:text-6xl lg:text-7xl font-bold uppercase tracking-tight mb-5 animate-fade-in-up-delay-1">
            <span className="bg-gradient-to-r from-grape-300 via-grape-400 to-purple-400 bg-clip-text text-transparent">
              Graph-proven
            </span>
            <br />
            <span className="text-zinc-100">AI code review</span>
          </h1>

          <p className="text-base sm:text-lg text-zinc-300 max-w-md sm:max-w-xl leading-relaxed mb-8 animate-fade-in-up-delay-2">
            Every finding cites the real import chain — not a guess.
            Installs as a GitHub App in 2&nbsp;minutes.
          </p>

          <div className="flex flex-col sm:flex-row gap-3 w-full sm:w-auto justify-center animate-fade-in-up-delay-3">
            <a
              href={GH_APP}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center justify-center gap-2 px-6 py-3.5 rounded-xl bg-grape-600 hover:bg-grape-500 text-white font-semibold transition-all shadow-xl shadow-grape-900/40 text-sm"
            >
              <GhIcon />
              Install GitHub App — free
            </a>
            <a
              href="/compare"
              className="inline-flex items-center justify-center gap-2 px-6 py-3.5 rounded-xl border border-white/[0.08] hover:border-grape-500/40 bg-white/[0.04] hover:bg-white/[0.07] text-zinc-300 hover:text-white font-semibold transition-all text-sm"
            >
              vs CodeRabbit, CodeAnt &amp; more <ArrowRight size={14} />
            </a>
          </div>
        </div>
      </section>

      {/* ── FEATURE STRIP ────────────────────────────────────────────────── */}
      <section className="border-t border-zinc-800/50 px-5 sm:px-6 py-14 sm:py-20">
        <div className="max-w-4xl mx-auto grid grid-cols-1 sm:grid-cols-3 gap-8 sm:gap-6">
          {FEATURES.map(({ Icon, title, desc }) => (
            <div key={title} className="flex flex-col gap-3">
              <div className="w-9 h-9 rounded-lg bg-grape-500/10 border border-grape-500/20 flex items-center justify-center">
                <Icon size={17} className="text-grape-400" />
              </div>
              <p className="text-sm font-semibold text-white">{title}</p>
              <p className="text-sm text-zinc-400 leading-relaxed">{desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* ── FINAL CTA ────────────────────────────────────────────────────── */}
      <section className="px-5 sm:px-6 py-16 sm:py-28 border-t border-zinc-800/50 text-center relative overflow-hidden">
        <div className="absolute inset-0 pointer-events-none">
          <div className="absolute bottom-0 left-1/2 -translate-x-1/2 w-[600px] h-[300px] rounded-full bg-grape-500/6 blur-[80px]" />
        </div>
        <div className="relative z-10 max-w-xl mx-auto">
          <h2 className="font-sans text-2xl sm:text-4xl font-bold text-zinc-100 mb-4 tracking-tight">
            Your next PR deserves a
            <br />
            <span className="bg-gradient-to-r from-grape-300 via-grape-400 to-purple-400 bg-clip-text text-transparent">
              graph-proven review.
            </span>
          </h2>
          <p className="text-zinc-500 text-sm mb-8 leading-relaxed">
            Install in 2&nbsp;minutes. Free for open source. No credit card.
          </p>
          <a
            href={GH_APP}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center justify-center gap-2 px-7 py-3.5 rounded-xl bg-grape-600 hover:bg-grape-500 text-white font-semibold transition-all shadow-xl shadow-grape-900/40 text-sm w-full sm:w-auto"
          >
            <GhIcon />
            Install GitHub App — free
          </a>
        </div>
      </section>

      <Footer />
    </main>
  );
}

function GhIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className="w-4 h-4 flex-shrink-0">
      <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z" />
    </svg>
  );
}
