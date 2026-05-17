import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import { Network, Zap, DollarSign } from "lucide-react";

const STEPS = [
  {
    num: "01",
    title: "Install the GitHub App",
    desc: "One click. Choose repos. No tokens to paste, no scripts to run. Works on any GitHub org or personal account.",
  },
  {
    num: "02",
    title: "Open a pull request",
    desc: "GrapeRoot receives the webhook, fetches the diff, and starts reviewing within seconds.",
  },
  {
    num: "03",
    title: "Graph traces blast radius",
    desc: "The AST import graph maps every file and symbol touched by the PR — including upstream callers in other repos.",
  },
  {
    num: "04",
    title: "Get proven comments",
    desc: "Every CRITICAL finding cites the real import chain. One-click committable suggestions for every fix.",
  },
];

const ABOUT_CARDS = [
  {
    icon: Network,
    title: "Graph-proven findings",
    body: "GrapeRoot builds an AST import graph of your codebase before reviewing. Every blast-radius claim cites real file edges — not a hallucination. If the graph doesn't have the edge, we don't claim it.",
    stat: "0 graph-fabricated findings",
  },
  {
    icon: Zap,
    title: "Zero setup",
    body: "Install the GitHub App. Choose repos. Every new PR gets reviewed automatically — no YAML, no CLI, no configuration file needed to get started.",
    stat: "~60s to first comment",
  },
  {
    icon: DollarSign,
    title: "Usage-based pricing",
    body: "~$0.05 per review in API costs. We pass most of the savings to you. At $15/user/month for unlimited reviews, the math works for any team size.",
    stat: "$0.05 avg cost / review",
  },
];

const FINDINGS = [
  { sev: "CRITICAL", repo: "grafana/loki",  finding: "ingestedAt hardcoded to 0 — destroys ingestion-time retention after first compaction" },
  { sev: "CRITICAL", repo: "grafana/tempo", finding: "Module directive missing /v3 suffix — every downstream importer breaks silently" },
  { sev: "HIGH",     repo: "grafana/loki",  finding: "Metrics registered before ring/KV init — duplicate-registration panic on retry" },
  { sev: "HIGH",     repo: "grafana/mimir", finding: "Breaking-change entry omits upgrade action required by operators" },
  { sev: "HIGH",     repo: "grafana/loki",  finding: "No test coverage for mixed-version (rolling-upgrade) cluster behaviour" },
];

export default function HowPage() {
  return (
    <main className="min-h-screen text-white">
      <Navbar />

      {/* ── HOW IT WORKS ─────────────────────────────────────────────────── */}
      <section className="relative px-4 pt-36 pb-24">
        <div className="max-w-5xl mx-auto">
          <p className="text-center text-[11px] font-bold text-grape-500 uppercase tracking-widest mb-3">
            How it works
          </p>
          <h1 className="font-sans text-center text-4xl sm:text-5xl font-bold text-zinc-100 mb-4 tracking-tight">
            Installs in 2 minutes.<br />Reviews every PR automatically.
          </h1>
          <p className="text-center text-zinc-400 text-base mb-14 max-w-md mx-auto leading-relaxed">
            No YAML to write. No CLI to run. Just install the GitHub App and open a PR.
          </p>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {STEPS.map((step) => (
              <div
                key={step.num}
                className="rounded-xl border border-white/[0.06] bg-white/[0.025] p-6 hover:border-grape-500/30 transition-colors"
              >
                <div className="w-8 h-8 rounded-lg bg-grape-500/10 border border-grape-500/20 flex items-center justify-center text-grape-300 text-xs font-bold mb-4">
                  {step.num}
                </div>
                <h3 className="font-sans text-sm font-bold text-zinc-100 mb-2">{step.title}</h3>
                <p className="text-xs text-zinc-500 leading-relaxed">{step.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── ABOUT ────────────────────────────────────────────────────────── */}
      <section className="px-4 py-24 border-t border-zinc-800/50">
        <div className="max-w-5xl mx-auto">
          <p className="text-center text-[11px] font-bold text-grape-500 uppercase tracking-widest mb-3">
            About
          </p>
          <h2 className="font-sans text-center text-3xl sm:text-4xl font-bold text-zinc-100 mb-4 tracking-tight">
            AI code review that proves every finding
          </h2>
          <p className="text-center text-zinc-400 text-sm mb-14 max-w-2xl mx-auto leading-relaxed">
            Most AI reviewers guess at blast radius. GrapeRoot traces the real import graph
            before posting a single comment. Every CRITICAL finding includes the exact call
            chain — auditable, not vibes.
          </p>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-16">
            {ABOUT_CARDS.map(({ icon: Icon, title, body, stat }) => (
              <div key={title} className="rounded-xl border border-white/[0.06] bg-white/[0.025] p-6 flex flex-col hover:border-grape-500/30 transition-colors">
                <div className="w-9 h-9 rounded-lg bg-grape-500/10 border border-grape-500/20 flex items-center justify-center text-grape-300 mb-4 flex-shrink-0">
                  <Icon size={18} />
                </div>
                <h3 className="font-sans text-sm font-bold text-zinc-100 mb-2">{title}</h3>
                <p className="text-xs text-zinc-500 leading-relaxed flex-1">{body}</p>
                <div className="mt-4 text-[11px] font-semibold text-grape-400 tracking-wide">{stat}</div>
              </div>
            ))}
          </div>

          {/* Production findings strip */}
          <div className="rounded-xl border border-white/[0.07] overflow-hidden">
            <div className="px-5 py-3 bg-[#0e0e11] border-b border-white/[0.07] flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-grape-500/70" />
              <span className="text-xs font-semibold text-zinc-300">Real findings from production PRs</span>
            </div>
            <table className="w-full text-xs">
              <tbody>
                {FINDINGS.map((f, i) => (
                  <tr key={i} className={`border-b border-white/[0.04] last:border-0 ${i % 2 ? "bg-zinc-900/20" : ""}`}>
                    <td className="px-4 py-3 w-24">
                      <span className={`inline-block text-[10px] font-bold px-2 py-0.5 rounded tracking-wider ${
                        f.sev === "CRITICAL" ? "bg-red-500/10 text-red-400" : "bg-orange-500/10 text-orange-400"
                      }`}>{f.sev}</span>
                    </td>
                    <td className="px-4 py-3 text-zinc-500 font-mono w-36">{f.repo}</td>
                    <td className="px-4 py-3 text-zinc-300">{f.finding}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="px-5 py-3 bg-[#0e0e11] border-t border-white/[0.07]">
              <p className="text-[11px] text-zinc-600">Caught on real production PRs across Grafana&apos;s LGTM stack (73,000+ files, 3 repos)</p>
            </div>
          </div>
        </div>
      </section>

      <Footer />
    </main>
  );
}
