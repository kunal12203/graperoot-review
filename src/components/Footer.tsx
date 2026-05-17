import { Twitter, Linkedin, Github } from "lucide-react";

const GH_APP = "https://github.com/apps/graperoot-review/installations/new/permissions?target_id=84341876";

const SOCIALS = [
  { label: "Twitter / X", href: "https://x.com/krishna37930189",             Icon: Twitter  },
  { label: "LinkedIn",    href: "https://www.linkedin.com/company/graperoot", Icon: Linkedin },
  { label: "GitHub",      href: "https://github.com/apps/graperoot-review",   Icon: Github   },
];

const PRODUCT = [
  { label: "About",     href: "/how"       },
  { label: "Compare",   href: "/compare"   },
  { label: "Pricing",   href: "/pricing"   },
  { label: "Dashboard", href: "/dashboard" },
];

const GET_STARTED = [
  { label: "Install GitHub App", href: GH_APP,                        accent: true  },
  { label: "Contact",            href: "mailto:hello@graperoot.dev",   accent: false },
  { label: "Privacy",            href: "/privacy",                     accent: false },
  { label: "Terms",              href: "/terms",                       accent: false },
];

export default function Footer() {
  return (
    <footer className="relative border-t border-zinc-800 bg-[#0a0a0d]/95 backdrop-blur-xl py-10 sm:py-12 px-4 sm:px-6">
      <span className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-white/10 to-transparent" />
      <div className="max-w-7xl mx-auto">
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-10 mb-10">

          <div className="flex flex-col gap-4">
            <a href="/" className="flex items-center gap-2.5 group w-fit">
              <div className="w-7 h-7 rounded-lg bg-grape-500/15 border border-grape-500/25 flex items-center justify-center overflow-hidden group-hover:bg-grape-500/25 transition-all">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src="/image.svg" alt="GrapeRoot" className="w-5 h-5" />
              </div>
              <span className="font-semibold text-white tracking-tight">GrapeRoot Review</span>
            </a>
            <p className="text-xs text-zinc-500 leading-relaxed max-w-[220px]">
              Graph-proven AI code review. Every finding cites the real import chain.
            </p>
            <div className="flex items-center gap-2">
              {SOCIALS.map(({ label, href, Icon }) => (
                <a
                  key={label}
                  href={href}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={label}
                  className="w-9 h-9 inline-flex items-center justify-center rounded-lg border border-zinc-800 bg-[#101015] text-zinc-400 hover:text-grape-300 hover:border-grape-500/50 hover:bg-grape-500/10 transition-all shadow-sm shadow-black/30"
                >
                  <Icon size={15} />
                </a>
              ))}
            </div>
          </div>

          <div>
            <p className="text-[11px] font-semibold text-zinc-500 uppercase tracking-widest mb-3">Product</p>
            <ul className="flex flex-col gap-2.5">
              {PRODUCT.map(({ label, href }) => (
                <li key={label}>
                  <a href={href} className="text-sm text-zinc-500 hover:text-zinc-300 transition-colors">
                    {label}
                  </a>
                </li>
              ))}
            </ul>
          </div>

          <div>
            <p className="text-[11px] font-semibold text-zinc-500 uppercase tracking-widest mb-3">Get started</p>
            <ul className="flex flex-col gap-2.5">
              {GET_STARTED.map(({ label, href, accent }) => (
                <li key={label}>
                  <a
                    href={href}
                    target={href.startsWith("http") ? "_blank" : undefined}
                    rel={href.startsWith("http") ? "noopener noreferrer" : undefined}
                    className={`text-sm transition-colors ${
                      accent ? "text-grape-400 hover:text-grape-300" : "text-zinc-500 hover:text-zinc-300"
                    }`}
                  >
                    {label}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        </div>

        <div className="pt-6 border-t border-zinc-800/40 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
          <p className="text-zinc-700 text-xs">© {new Date().getFullYear()} GrapeRoot. All rights reserved.</p>
          <p className="text-zinc-700 text-xs">review.graperoot.dev</p>
        </div>
      </div>
    </footer>
  );
}
